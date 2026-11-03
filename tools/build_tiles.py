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

async def build(page, tiles, out, max_minutes=300, delay=2.0, tile_timeout=1500, log=print, cars=None, pois=True, rest_base=120, coverage=None):
    """page: a page that has the planner loaded – or a LIST of such pages: then that many areas are processed at the same time
    (only useful with a map server of our own, see --overpass-local). Returns (done, failed, skipped) – AREAS."""
    pages = page if isinstance(page, (list, tuple)) else [page]
    page = pages[0]
    # coverage(i, j) -> True when the map data really covers that area (own server only). Areas outside the extract would come out
    # EMPTY ("0 stretches, 0 places") and must not be stored as finished.
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

    cov_cache = {}
    async def covered(i, j):
        if not coverage:
            return True
        k = (i, j)
        if k not in cov_cache:
            try:
                cov_cache[k] = bool(await coverage(i, j))
            except Exception:
                cov_cache[k] = True      # cannot tell → treat as covered
        return cov_cache[k]
    nodata = []
    if coverage:
        # results that an earlier run stored for areas WITHOUT map data (0 stretches / 0 places) are removed again
        purged = 0
        for cid in cars:
            ce = man['cars'][keys[cid]]
            for k, v in list(ce['tiles'].items()):
                if v.get('n', 1) == 0 and v.get('km', 1) == 0:
                    i, j = (int(x) for x in k.split(','))
                    if not await covered(i, j):
                        del ce['tiles'][k]
                        try:
                            os.remove(os.path.join(out, car_dir(cid), f"{i}_{j}.json"))
                        except OSError:
                            pass
                        purged += 1
        if pois:
            for k, n in list(man['poi']['tiles'].items()):
                if n == 0:
                    i, j = (int(x) for x in k.split(','))
                    if not await covered(i, j):
                        del man['poi']['tiles'][k]
                        try:
                            os.remove(os.path.join(out, 'p', f"{i}_{j}.json"))
                        except OSError:
                            pass
                        purged += 1
        if purged:
            log(f"{purged} empty results of areas without map data removed again")
    t0 = time.time()
    todo = [(i, j) for (i, j) in tiles if missing(i, j)]
    skipped = len(tiles) - len(todo)
    done, saved_n = [], 0
    out_of_time = False
    # several passes: every area gets ONE try per pass; failures wait for the next pass, so one overloaded
    # server does not eat the whole time budget. Between passes the robot rests so the servers can recover.
    # With several pages (own map server) the areas of a pass are shared between the pages.
    par = len(pages) > 1
    for rnd in range(1, 5):
        if not todo or out_of_time:
            break
        if rnd > 1:
            rest = (rest_base if not par else min(rest_base, 20)) * (rnd - 1)
            log(f"pass {rnd}: {len(todo)} areas left – resting {rest} s so the map servers can recover")
            await asyncio.sleep(rest)
        retry, state = [], {'streak': 0}
        queue = asyncio.Queue()
        for it in todo:
            queue.put_nowait(it)

        async def one_area(pg, i, j):
            nonlocal saved_n, out_of_time
            k = f"{i},{j}"
            if (time.time() - t0) / 60 > max_minutes:
                if not out_of_time:
                    log(f"time budget of {max_minutes} min used up – stopping; run again to continue")
                out_of_time = True
                retry.append((i, j))
                return
            if not await covered(i, j):
                if k not in nodata:
                    nodata.append(k)
                    log(f"[{k}] no map data here (outside the map extract) – skipped")
                return
            progress = False
            for part in missing(i, j):
                try:
                    if part == 'poi':
                        res = await asyncio.wait_for(pg.evaluate("([i,j])=>__flrp.exPrebuildPois(i,j)", [i, j]), tile_timeout)
                        with open(os.path.join(out, 'p', f"{i}_{j}.json"), 'w', encoding='utf-8') as f:
                            json.dump({'pois': res['pois']}, f, separators=(',', ':'), ensure_ascii=False)
                        man['poi']['tiles'][k] = res['n']
                        man['poi']['built'] = now()
                        log(f"[{k}] places: {res['n']}")
                    else:
                        await pg.evaluate("id=>__flrp.exSetCar(id)", part)
                        res = await asyncio.wait_for(pg.evaluate("([i,j])=>__flrp.exPrebuild(i,j)", [i, j]), tile_timeout)
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
                state['streak'] = 0 if progress else state['streak'] + 1
                if state['streak'] >= 3 and not par:
                    log("  3 failures in a row – short rest")
                    await asyncio.sleep(90)
                    state['streak'] = 0
            else:
                state['streak'] = 0
                done.append(k)
                try:
                    await pg.evaluate("([i,j])=>__flrp.exForget(i,j)", [i, j])   # free the memory of this area
                except Exception:
                    pass
                log(f"[{skipped + len(done)}/{len(tiles)}] area {k} complete")
                await asyncio.sleep(delay)

        async def worker(pg):
            while True:
                try:
                    i, j = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                await one_area(pg, i, j)

        await asyncio.gather(*[worker(pg) for pg in pages])
        todo = retry
    if nodata:
        log(f"{len(nodata)} areas have no map data in the extract and were skipped (add the neighbouring region to extract_url to cover them)")
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

async def local_status_ok(url, tries=1, wait=0):
    """is our own Overpass server answering? (a tiny real query – the image's /api/status page is not reliable)"""
    import urllib.request, urllib.parse
    q = url + '?' + urllib.parse.urlencode({'data': '[out:json];node(1);out;'})
    for n in range(tries):
        try:
            with urllib.request.urlopen(q, timeout=20) as r:
                if r.status == 200 and '"elements"' in r.read().decode('utf-8', 'replace'):
                    return True
        except Exception:
            pass
        if n + 1 < tries:
            await asyncio.sleep(wait)
    return False

async def count_ways(local, i, j):
    """number of roads in one area according to our own server (0-few = the extract does not cover the area)"""
    import urllib.request, urllib.parse
    s, w, n, e = i * TLAT, j * TLON, (i + 1) * TLAT, (j + 1) * TLON
    q = f'[out:json][timeout:90];way["highway"]({s:.4f},{w:.4f},{n:.4f},{e:.4f});out count;'
    def go():
        req = urllib.request.Request(local, data=urllib.parse.urlencode({'data': q}).encode())
        with urllib.request.urlopen(req, timeout=120) as r:
            j_ = json.loads(r.read().decode('utf-8', 'replace'))
        for el in j_.get('elements', []):
            if el.get('type') == 'count':
                return int(el['tags'].get('ways', el['tags'].get('total', 0)))
        raise RuntimeError('no count in answer')
    return await asyncio.to_thread(go)

MIN_WAYS = 30      # an 11 x 11 km area with fewer roads than this is outside the extract

def make_coverage(local):
    async def cov(i, j):
        return (await count_ways(local, i, j)) >= MIN_WAYS
    return cov

def make_elev_handler(gap=1.5, log=print, retries=3, wait=20):
    """The terrain-height service (Open-Meteo) allows only a few requests per minute. All requests of all pages go through ONE queue
    with a minimum gap, and a 429 answer is waited out and repeated instead of being handed to the page as a failure."""
    state = {'last': 0.0}
    lock = asyncio.Lock()
    async def handler(route):
        async with lock:
            pause = state['last'] + gap - time.time()
            if pause > 0:
                await asyncio.sleep(pause)
            resp = None
            for attempt in range(retries + 1):
                try:
                    resp = await route.fetch(timeout=60000)
                except Exception:
                    state['last'] = time.time()
                    await route.abort()
                    return
                if resp.status == 429 and attempt < retries:
                    log(f"  terrain service said 'too many requests' – waiting {wait * (attempt + 1)} s")
                    await asyncio.sleep(wait * (attempt + 1))
                    continue
                break
            state['last'] = time.time()
        await route.fulfill(response=resp)
    return handler

async def install_elev_route(ctx, shared_handler):
    await ctx.route(lambda u: 'api.open-meteo.com' in u, shared_handler)

# Terrain heights from the free AWS "terrarium" height tiles (worldwide, no key, no limit) instead of Open-Meteo (429 when fast).
# Answers the planner's request in the same format; if anything fails the request falls through to the Open-Meteo queue above.
TERRAIN_URL = 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'

def _fetch_bytes(url, tries=4):
    import urllib.request
    last = None
    for n in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'fairlane-rallye-planner-build'})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(1.5 * (n + 1))
    raise last

class Terrain:
    def __init__(self, fetch=None, z=12):
        self.fetch = fetch or (lambda z, x, y: _fetch_bytes(TERRAIN_URL.format(z=z, x=x, y=y)))
        self.z = z
        self.cache = {}
        self.lock = threading.Lock()

    def _tile(self, x, y):
        with self.lock:
            if (x, y) in self.cache:
                return self.cache[(x, y)]
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(self.fetch(self.z, x, y))).convert('RGB')
        img.load()
        with self.lock:
            if len(self.cache) > 300:
                self.cache.pop(next(iter(self.cache)))
            self.cache[(x, y)] = img
        return img

    def height(self, lat, lon):
        n = 2 ** self.z
        fx = (lon + 180.0) / 360.0 * n
        lr = math.radians(lat)
        fy = (1 - math.log(math.tan(lr) + 1 / math.cos(lr)) / math.pi) / 2 * n
        x, y = int(fx), int(fy)
        px, py = min(255, int((fx - x) * 256)), min(255, int((fy - y) * 256))
        r, g, b = self._tile(x, y).getpixel((px, py))
        return round(r * 256 + g + b / 256 - 32768, 1)

async def install_terrain_route(ctx, terrain, log=print):
    import urllib.parse
    stats = {'n': 0, 'err': 0}
    async def handler(route):
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(route.request.url).query)
            la = [float(v) for v in q['latitude'][0].split(',')]
            lo = [float(v) for v in q['longitude'][0].split(',')]
            el = await asyncio.to_thread(lambda: [terrain.height(a, b) for a, b in zip(la, lo)])
            stats['n'] += 1
            await route.fulfill(status=200, content_type='application/json', headers={'access-control-allow-origin': '*'}, body=json.dumps({'elevation': el}))
        except Exception as e:
            stats['err'] += 1
            log(f"  height tiles failed ({str(e)[:80]}) – asking Open-Meteo instead")
            try:
                await route.fallback()
            except Exception:
                pass
    await ctx.route(lambda u: u.startswith('https://api.open-meteo.com/v1/elevation'), handler)
    return stats

async def install_local_route(ctx, local, log=print):
    """Every Overpass request of the planner is answered by OUR server. The planner asks several public servers one after another
    ('hedged'); only the first one is forwarded, the others are cut off at once so our server never gets the same question twice."""
    primary = 'overpass-api.de'
    stats = {'n': 0, 'err': 0}
    async def handler(route):
        req = route.request
        host = req.url.split('/')[2]
        if host != primary:
            await route.abort()
            return
        stats['n'] += 1
        try:
            if req.method == 'GET' and '?' in req.url:
                target = local + '?' + req.url.split('?', 1)[1]
                resp = await route.fetch(url=target, timeout=240000)
            else:
                resp = await route.fetch(url=local, method='POST', post_data=req.post_data, headers={'content-type': 'application/x-www-form-urlencoded;charset=UTF-8'}, timeout=240000)
            hdr = {k: v for k, v in resp.headers.items() if k.lower() not in ('content-encoding', 'content-length', 'transfer-encoding')}
            hdr['access-control-allow-origin'] = '*'
            await route.fulfill(status=resp.status, headers=hdr, body=await resp.body())
        except Exception as e:
            stats['err'] += 1
            log(f"  local map server did not answer: {str(e)[:100]}")
            try:
                await route.abort()
            except Exception:
                pass
    await ctx.route(lambda u: ('/api/interpreter' in u) and ('overpass' in u), handler)
    return stats

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--html', default='index.html')
    ap.add_argument('--out', default='data')
    ap.add_argument('--center', default='48.1786,11.2556')
    ap.add_argument('--radius-km', type=float, default=50)
    ap.add_argument('--bbox', help='south,west,north,east (instead of centre/radius)')
    ap.add_argument('--max-minutes', type=float, default=300)
    ap.add_argument('--delay', type=float, default=2.0)
    ap.add_argument('--print-bbox', action='store_true', help='print west,south,east,north of the region (plus one area of margin) and exit – used to cut the map extract for our own server')
    ap.add_argument('--overpass-local', help='URL of a map server of our own (…/api/interpreter): all map-data requests of the page go there instead of the public servers')
    ap.add_argument('--workers', type=int, default=0, help='areas processed at the same time (default: 1 with public servers, 3 with --overpass-local)')
    a = ap.parse_args()
    from playwright.async_api import async_playwright
    bbox = tuple(float(x) for x in a.bbox.split(',')) if a.bbox else None
    center = tuple(float(x) for x in a.center.split(','))
    tiles = tiles_for(center, a.radius_km, bbox)
    if a.print_bbox:
        ii = [i for i, _ in tiles]; jj = [j for _, j in tiles]
        print(f"{(min(jj) - 1) * TLON:.4f},{(min(ii) - 1) * TLAT:.4f},{(max(jj) + 2) * TLON:.4f},{(max(ii) + 2) * TLAT:.4f}")
        return
    print(f"{len(tiles)} areas in the region")
    html_dir = os.path.dirname(os.path.abspath(a.html)) or '.'
    httpd, port = serve(html_dir)
    local = a.overpass_local
    if local and not await local_status_ok(local, tries=3, wait=10):
        print(f"own map server {local} does not answer – using the public servers instead")
        local = None
    workers = a.workers or (3 if local else 1)
    delay = min(a.delay, 0.3) if local else a.delay
    print(f"map data from: {'OWN server ' + local if local else 'public servers'} · {workers} area(s) at a time")
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pages = []
        terrain = None
        try:
            import PIL  # noqa: F401
            terrain = Terrain()
        except ImportError:
            print('Pillow is not installed – terrain heights come from Open-Meteo (slow, may be refused)')
        elev = make_elev_handler(gap=1.5)       # ONE queue for the terrain service across all pages
        for w in range(workers):
            ctx = await b.new_context(viewport={'width': 1200, 'height': 800})
            await install_elev_route(ctx, elev)
            if terrain:
                await install_terrain_route(ctx, terrain)
            if local:
                await install_local_route(ctx, local)
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
            pages.append(page)
        done, failed, skipped = await build(pages, tiles, a.out, a.max_minutes, delay, coverage=make_coverage(local) if local else None)
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
