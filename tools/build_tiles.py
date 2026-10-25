#!/usr/bin/env python3
"""
Pre-classify map areas for the Fairlane Rallye Planner.

It opens the planner (index.html) in a headless Chrome, lets the app itself load and classify every map
area ("tile") of a region exactly like it does on your phone, and saves each result as a small JSON file:

    data/manifest.json        which areas are ready, for which car profile / algorithm version, and when
    data/t/<i>_<j>.json       the finished classification of one area

The app then reads these files first and only talks to the map servers for areas that are not ready.

Usage (the GitHub workflow does this for you):
    python tools/build_tiles.py --html index.html --out data --center 48.1786,11.2556 --radius-km 50

Re-running continues where it stopped: areas that are already in the manifest (same algorithm version) are skipped.
"""
import argparse, asyncio, json, math, os, sys, threading, time, datetime, http.server, socketserver, functools

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

async def build(page, tiles, out, max_minutes=300, delay=2.0, tile_timeout=600, log=print):
    """page: a page that has the planner loaded. Returns (done, failed, skipped)."""
    car_key = await page.evaluate("__flrp.exCarKey()")
    os.makedirs(os.path.join(out, 't'), exist_ok=True)
    mpath = os.path.join(out, 'manifest.json')
    man = load_manifest(mpath)
    if not man or man.get('carKey') != car_key:
        if man:
            log(f"algorithm / car profile changed ({man.get('carKey')} -> {car_key}): rebuilding everything")
            for fn in os.listdir(os.path.join(out, 't')):
                os.remove(os.path.join(out, 't', fn))
        man = {'v': 1, 'dir': 't', 'carKey': car_key, 'tiles': {}}
    t0 = time.time()
    done, failed, skipped = [], [], 0
    for n, (i, j) in enumerate(tiles):
        k = f"{i},{j}"
        if k in man['tiles']:
            skipped += 1
            continue
        if (time.time() - t0) / 60 > max_minutes:
            log(f"time budget of {max_minutes} min used up – stopping; run again to continue")
            break
        res = None
        for attempt in range(3):
            try:
                res = await asyncio.wait_for(page.evaluate("([i,j])=>__flrp.exPrebuild(i,j)", [i, j]), tile_timeout)
                break
            except Exception as e:
                log(f"  tile {k} attempt {attempt + 1} failed: {str(e)[:120]}")
                await asyncio.sleep(20 * (attempt + 1))
        if not res or not res.get('pack') or not res.get('topo') and res.get('usN'):
            failed.append(k)
            continue
        with open(os.path.join(out, 't', f"{i}_{j}.json"), 'w', encoding='utf-8') as f:
            json.dump(res['pack'], f, separators=(',', ':'))
        man['tiles'][k] = {'km': res.get('usKm', 0), 'n': res.get('usN', 0)}
        man['built'] = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        done.append(k)
        if len(done) % 5 == 0:
            save_manifest(mpath, man)
        log(f"[{n + 1}/{len(tiles)}] tile {k}: {res.get('usN', 0)} US-feeling stretches, {res.get('usKm', 0)} km")
        await asyncio.sleep(delay)
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
        await page.wait_for_function("window.__flrp&&window.__flrp.exPrebuild", timeout=60000)
        done, failed, skipped = await build(page, tiles, a.out, a.max_minutes, a.delay)
        await b.close()
    httpd.shutdown()
    print(f"finished: {len(done)} built, {skipped} already there, {len(failed)} failed")
    if failed:
        print("failed areas (run again to retry):", ' '.join(failed))

if __name__ == '__main__':
    asyncio.run(main())
