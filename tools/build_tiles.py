#!/usr/bin/env python3
"""
Pre-classify map areas for the Fairlane Rallye Planner.

It opens the planner (index.html) in a headless Chrome, lets the app itself load and classify every map
area ("tile") of a region exactly like it does on your phone, and saves each result as a small JSON file:

    data/manifest.json        which areas are ready, for which car profiles / algorithm version, and when
    data/t/<i>_<j>.json       the finished classification of one area for the Fairlane
    data/t_focus/<i>_<j>.json the same for the Ford Focus
    data/p/<i>_<j>.json       the places (POIs) of one area, shared by all cars

The app then reads these files first and only talks to the map servers for areas that are not ready.

Usage (the GitHub workflow does this for you):
    python tools/build_tiles.py --html index.html --out data --center 48.1786,11.2556 --radius-km 50

Re-running continues where it stopped: areas that are already in the manifest (same algorithm version) are skipped.
"""
import sys, argparse, asyncio, json, math, os, sys, threading, time, datetime, http.server, socketserver, functools

TLAT, TLON = 0.1, 0.15   # must match the app

def tiles_for(center=None, radius_km=None, bbox=None):
    """all (i, j) tiles that touch the circle (as a box) or the bbox s,w,n,e – nearest to the centre first"""
    if bbox:
        s, w, n, e = bbox
        c = ((s + n) / 2, (w + e) / 2)
    else:
        la, lo = center
        dlat = radius_km / 111.2
        dlon = radius_km / (111.2 * math.cos(math.radians(la)))
        s, n, w, e = la - dlat, la + dlat, lo - dlon, lo + dlon
        c = center
    out = []
    for i in range(math.floor(s / TLAT), math.floor(n / TLAT) + 1):
        for j in range(math.floor(w / TLON), math.floor(e / TLON) + 1):
            mid = ((i + .5) * TLAT, (j + .5) * TLON)
            out.append((math.hypot(mid[0] - c[0], (mid[1] - c[1]) * math.cos(math.radians(c[0]))), i, j))
    out.sort()
    return [(i, j) for _, i, j in out]

def load_manifest(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None

def save_manifest(path, man):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(man, f, separators=(',', ':'))
    os.replace(tmp, path)

CAR_IDS = ['fairlane58', 'focus']          # car profiles that get ready-made classification
CAR_DIR = {'fairlane58': 't'}               # folder per car (Fairlane keeps the old folder 't')

def car_dir(cid):
    return CAR_DIR.get(cid, 't_' + cid)

def clear_dir(path):
    if os.path.isdir(path):
        for fn in os.listdir(path):
            try:
                os.remove(os.path.join(path, fn))
            except OSError:
                pass

def normalise_manifest(man):
    """older manifests had ONE car (carKey/tiles/dir) – turn them into the several-cars format"""
    if man and man.get('carKey') and 'cars' not in man:
        man = {'v': 2, 'cars': {man['carKey']: {'dir': man.get('dir', 't'), 'tiles': man.get('tiles', {}), 'built': man.get('built')}}}
    man = man or {}
    man['v'] = 2
    man.setdefault('cars', {})
    return man

async def build(page, tiles, out, max_minutes=300, delay=2.0, tile_timeout=1500, log=print, cars=None, pois=True, rest_base=120):
    """page: a page that has the planner loaded. Returns (done, failed, skipped) – done/failed/skipped are AREAS."""
    cars = cars or CAR_IDS
    mpath = os.path.join(out, 'manifest.json')
    man = normalise_manifest(load_manifest(mpath))
    keys = {}
    for cid in cars:
        keys[cid] = await page.evaluate("id=>__flrp.exSetCar(id)", cid)
        os.makedirs(os.path.join(out, car_dir(cid)), exist_ok=True)
    # a car whose algorithm changed: the old results are KEPT as a fallback (the app uses them for areas that are not rebuilt yet,
    # the files are overwritten area by area) and dropped from the manifest as soon as the new ones are complete – see the end
    for key in list(man['cars']):
        if key not in keys.values():
            log(f"older results kept as a fallback until the new ones are complete: {key[:40]}…")
    for cid in cars:
        man['cars'].setdefault(keys[cid], {'dir': car_dir(cid), 'tiles': {}, 'built': None})['dir'] = car_dir(cid)
    if pois:
        pver = await page.evaluate("__flrp.exPoiVer()")
        pe = man.get('poi')
        if not pe or pe.get('ver') != pver:
            if pe:
                log("POI definitions changed – rebuilding the places")
                clear_dir(os.path.join(out, pe.get('dir', 'p')))
            pe = man['poi'] = {'dir': 'p', 'ver': pver, 'tiles': {}, 'built': None}
        os.makedirs(os.path.join(out, 'p'), exist_ok=True)
    now = lambda: datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    def missing(i, j):
        k = f"{i},{j}"
        m = [cid for cid in cars if man['cars'][keys[cid]]['tiles'].get(k, {}).get('v') != 2]   # entries without v:2 come from an older, less strict robot → rebuilt
        if pois and k not in man['poi']['tiles']:
            m.append('poi')
        return m

    t0 = time.time()
    todo = [(i, j) for (i, j) in tiles if missing(i, j)]
    skipped = len(tiles) - len(todo)
    done, saved_n = [], 0
    out_of_time = False
    # several passes: every area gets ONE try per pass; failures wait for the next pass, so one overloaded
    # server does not eat the whole time budget. Between passes the robot rests so the servers can recover.
    for rnd in range(1, 5):
        if not todo or out_of_time:
            break
        if rnd > 1:
            rest = rest_base * (rnd - 1)
            log(f"pass {rnd}: {len(todo)} areas left – resting {rest} s so the map servers can recover")
            await asyncio.sleep(rest)
        retry, streak = [], 0
        for (i, j) in todo:
            k = f"{i},{j}"
            if (time.time() - t0) / 60 > max_minutes:
                log(f"time budget of {max_minutes} min used up – stopping; run again to continue")
                out_of_time = True
                retry.append((i, j))
                continue
            progress = False
            for part in missing(i, j):
                try:
                    if part == 'poi':
                        res = await asyncio.wait_for(page.evaluate("([i,j])=>__flrp.exPrebuildPois(i,j)", [i, j]), tile_timeout)
                        with open(os.path.join(out, 'p', f"{i}_{j}.json"), 'w', encoding='utf-8') as f:
                            json.dump({'pois': res['pois']}, f, separators=(',', ':'), ensure_ascii=False)
                        man['poi']['tiles'][k] = res['n']
                        man['poi']['built'] = now()
                        log(f"[{k}] places: {res['n']}")
                    else:
                        await page.evaluate("id=>__flrp.exSetCar(id)", part)
                        res = await asyncio.wait_for(page.evaluate("([i,j])=>__flrp.exPrebuild(i,j)", [i, j]), tile_timeout)
                        if not res or not res.get('pack'):
                            raise RuntimeError('incomplete result')
                        if not res.get('landOk') or not res.get('bldOk') or not res.get('xd') or not res.get('topoOk'):
                            # land use / extras / terrain data missing: the result would look worse (or better) than it is
                            raise RuntimeError('data incomplete (land %s, buildings %s, extras %s, terrain %s) – will retry' % (res.get('landOk'), res.get('bldOk'), res.get('xd'), res.get('topoOk')))
                        d = car_dir(part)
                        with open(os.path.join(out, d, f"{i}_{j}.json"), 'w', encoding='utf-8') as f:
                            json.dump(res['pack'], f, separators=(',', ':'))
                        ce = man['cars'][keys[part]]
                        ce['tiles'][k] = {'km': res.get('usKm', 0), 'n': res.get('usN', 0), 'v': 2}
                        ce['built'] = now()
                        log(f"[{k}] {part}: {res.get('usN', 0)} US-feeling stretches, {res.get('usKm', 0)} km")
                    progress = True
                    saved_n += 1
                    if saved_n % 5 == 0:
                        man['built'] = now()
                        save_manifest(mpath, man)
                except Exception as e:
                    log(f"  area {k} / {part} failed (pass {rnd}): {str(e)[:100]}")
            if missing(i, j):
                retry.append((i, j))
                streak = 0 if progress else streak + 1
                if streak >= 3:
                    log("  3 failures in a row – short rest")
                    await asyncio.sleep(90)
                    streak = 0
            else:
                streak = 0
                done.append(k)
                try:
                    await page.evaluate("([i,j])=>__flrp.exForget(i,j)", [i, j])   # free the memory of this area
                except Exception:
                    pass
                log(f"[{skipped + len(done)}/{len(tiles)}] area {k} complete")
                await asyncio.sleep(delay)
        todo = retry
    failed = [f"{i},{j}" for (i, j) in todo]
    # new results complete for every requested area of a car → its older versions are no longer needed
    for cid in cars:
        cur = man['cars'][keys[cid]]
        if all(cur['tiles'].get(f"{i},{j}", {}).get('v') == 2 for (i, j) in tiles):
            for key in list(man['cars']):
                if key not in keys.values() and man['cars'][key].get('dir') == cur.get('dir'):
                    man['cars'].pop(key)
                    log(f"new results complete – older version dropped from the manifest: {key[:40]}…")
    man['built'] = now()
    save_manifest(mpath, man)
    return done, failed, skipped

def serve(directory):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    class Q(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a): pass
    httpd = socketserver.TCPServer(('127.0.0.1', 0), functools.partial(Q, directory=directory))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--html', default='index.html')
    ap.add_argument('--out', default='data')
    ap.add_argument('--center', default='48.1786,11.2556')
    ap.add_argument('--radius-km', type=float, default=50)
    ap.add_argument('--bbox', help='south,west,north,east (instead of centre/radius)')
    ap.add_argument('--max-minutes', type=float, default=300)
    ap.add_argument('--delay', type=float, default=2.0)
    a = ap.parse_args()
    from playwright.async_api import async_playwright
    bbox = tuple(float(x) for x in a.bbox.split(',')) if a.bbox else None
    center = tuple(float(x) for x in a.center.split(','))
    tiles = tiles_for(center, a.radius_km, bbox)
    print(f"{len(tiles)} areas in the region")
    html_dir = os.path.dirname(os.path.abspath(a.html)) or '.'
    httpd, port = serve(html_dir)
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={'width': 1200, 'height': 800})
        page = await ctx.new_page()
        page.on('pageerror', lambda e: print('page error:', e))
        await page.goto(f"http://127.0.0.1:{port}/{os.path.basename(a.html)}")
        page.on('console', lambda m: print('page console:', m.type, m.text[:300]) if m.type in ('error', 'warning') else None)
        try:
            await page.wait_for_function("(()=>{try{return typeof window.__flrp.exPrebuild==='function'}catch(e){return false}})()", timeout=240000, polling=1000)
        except Exception:
            info = await page.evaluate("({leaflet: typeof L, flrp: typeof window.__flrp, prebuild: window.__flrp ? typeof window.__flrp.exPrebuild : 'n/a', title: document.title})")
            print('\nThe planner page did not become ready:', info)
            if info.get('prebuild') == 'function':
                print('-> The page became ready only after the wait ran out (slow start). Just run again.')
            elif info.get('leaflet') == 'undefined':
                print('-> The map library (Leaflet) could not be loaded from the internet.')
            elif info.get('prebuild') != 'function':
                print('-> index.html in the repository is an OLD planner version without the build hook.')
                print('   Upload the newest planner file again, named exactly index.html (Add file -> Upload files, same name replaces it).')
            await b.close()
            sys.exit(1)
        done, failed, skipped = await build(page, tiles, a.out, a.max_minutes, a.delay)
        await b.close()
    httpd.shutdown()
    print(f"finished: {len(done)} built, {skipped} already there, {len(failed)} failed")
    try:
        open('failed_count.txt', 'w').write(str(len(failed)))   # read by the workflow: decides whether another run is needed
    except Exception:
        pass
    if failed:
        print("failed areas (run again to retry):", ' '.join(failed))

if __name__ == '__main__':
    asyncio.run(main())
