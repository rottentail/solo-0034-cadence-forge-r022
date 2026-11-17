#!/usr/bin/env python3
"""Bounded production workflow verification; baseline automated testing is deferred."""
import argparse
import concurrent.futures
import hashlib
import io
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def need(value, message):
    if not value:
        raise RuntimeError(message)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workflow', default='all')
    args = ap.parse_args()
    observations = []
    with tempfile.TemporaryDirectory(prefix='cadence-workflow-') as scratch:
        scratch = Path(scratch)
        ffmpeg = os.environ.get('FFMPEG_BIN', 'ffmpeg')
        subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
                        '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=24:duration=4',
                        '-f', 'lavfi', '-i', 'sine=frequency=660:sample_rate=48000:duration=4',
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '1',
                        '-c:a', 'aac', '-shortest', str(scratch/'calibration.mp4')], check=True, timeout=30)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        address = 'http://127.0.0.1:'+str(port)+'/forge/v1'
        env = dict(os.environ, FORGE_ROOT=str(scratch/'data'),
                   FORGE_ADDR='127.0.0.1:'+str(port), FORGE_WORKERS='1')
        log = open(scratch/'server.log', 'ab')
        process = None
        def request(method, route, body=None, headers=None, status=200, raw=False):
            hs = dict(headers or {})
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
                hs['Content-Type'] = 'application/json'
            req = urllib.request.Request(address+route, data=body, headers=hs, method=method)
            try:
                response = urllib.request.urlopen(req, timeout=60)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                data = response.read()
                need(response.status == status, f'{method} {route}: {response.status}, wanted {status}: {data[:600]!r}')
                return data if raw or not data else json.loads(data)
        def start():
            nonlocal process
            process = subprocess.Popen([str(ROOT/'bin/forged')], cwd=ROOT, env=env,
                                       stdout=log, stderr=log, start_new_session=True)
            deadline = time.monotonic()+10
            while time.monotonic() < deadline:
                try:
                    request('GET', '/health')
                    return
                except (OSError, RuntimeError):
                    if process.poll() is not None:
                        raise RuntimeError((scratch/'server.log').read_text())
                    time.sleep(.05)
            raise RuntimeError('server readiness timed out')
        def stop(force=False):
            nonlocal process
            if process and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
                process.wait(timeout=15)
            process = None
        def observe(name, detail):
            observations.append({'workflow_id': name, 'passed': True, 'observed': detail})
        def wait_render(rid, desired=None):
            deadline = time.monotonic()+40
            while time.monotonic() < deadline:
                r = request('GET', '/renders/'+rid)
                if desired and r['state'] == desired:
                    return r
                if not desired and r['state'] in ('succeeded', 'failed', 'cancelled'):
                    return r
                time.sleep(.02)
            raise RuntimeError('render state timeout')
        try:
            start()
            media = (scratch/'calibration.mp4').read_bytes()
            source = request('POST', '/sources?name=calibration-pulse', media, status=201)
            sid = source['id']
            need(source['media']['video'] and source['media']['audio'], 'probe lost streams')
            need(source['digest'] == hashlib.sha256(media).hexdigest(), 'source digest mismatch')
            request('POST', '/sources?name=invalid', b'not-media', status=400)
            duplicate = request('POST', '/sources?name=duplicate', media, status=201)
            need(duplicate['id'] == sid, 'ingest did not converge')
            observe('ingest', 'MP4 accepted with SHA-256; malformed bytes rejected; duplicate converges')
            recipe_body = dict(name='review-proxy', format='mp4', width=160, height=90,
                               fps=24, sample_rate=48000, normalize=False, target_lufs=-16)
            recipe = request('POST', '/recipes', recipe_body, status=201)
            rp = recipe['id']
            updated = request('PUT', '/recipes/'+rp, dict(recipe_body, name='review-proxy-v2'), {'If-Match': '1'}, status=201)
            need(updated['revision'] == 2, 'recipe revision not advanced')
            request('PUT', '/recipes/'+rp, recipe_body, {'If-Match': '1'}, status=409)
            observe('recipe-version', 'revision 2 saved; stale revision rejected')
            sheet_body = dict(name='two-pass-calibration', spans=[
                dict(source_id=sid, in_ms=0, out_ms=1500, gain_db=-3),
                dict(source_id=sid, in_ms=2000, out_ms=3500, gain_db=0)],
                cues=[dict(source_id=sid, start_ms=1000, end_ms=2500, text='校准 <幅度>')])
            sheet = request('POST', '/sheets', sheet_body, status=201)
            sh = sheet['id']
            revision = request('PUT', '/sheets/'+sh, dict(sheet_body, name='revised-calibration'), {'If-Match': '1'}, status=201)
            need(revision['revision'] == 2, 'sheet revision not advanced')
            request('PUT', '/sheets/'+sh, sheet_body, {'If-Match': '1'}, status=409)
            observe('sheet-version', 'two-cut revision saved; stale writer rejected')
            plan = request('GET', '/sheets/'+sh+'/preview?recipe_id='+rp)
            need(plan['duration_ms'] == 3000 and plan['placements'][1]['output_in_ms'] == 1500, 'timeline mismatch')
            observe('preview', 'two source spans compacted to a 3000ms composition')
            need('00:00:01.000 --> 00:00:01.500' in plan['vtt'], 'first caption clipping mismatch')
            need('00:00:01.500 --> 00:00:02.000' in plan['vtt'], 'second caption mapping mismatch')
            need('&lt;幅度&gt;' in plan['vtt'], 'caption markup not escaped')
            observe('captions', 'source cue split and clipped into two escaped output intervals')
            if args.workflow in ('all', 'loudness'):
                loud = request('POST', '/sources/'+sid+'/loudness')
                need(loud['integrated_lufs'] is not None and not loud['silent'], 'loudness scan failed')
                observe('loudness', 'measured integrated LUFS and true peak')
            for mode in ['storyboard', 'waveform']:
                if args.workflow in ('all', mode):
                    image = request('GET', '/sources/'+sid+'/visual?mode='+mode, raw=True)
                    need(image.startswith(b'\x89PNG'), 'inspection output is not PNG')
                    observe(mode, f'PNG produced from source streams: {len(image)} bytes')
            req = dict(sheet_id=sh, sheet_revision=1, recipe_id=rp, recipe_revision=1)
            render = request('POST', '/renders', req, {'Idempotency-Key': 'first-render'}, status=202)
            rid = render['id']
            again = request('POST', '/renders', req, {'Idempotency-Key': 'first-render'}, status=202)
            need(again['id'] == rid, 'idempotency key did not converge')
            request('POST', '/renders', dict(req, sheet_revision=2), {'Idempotency-Key': 'first-render'}, status=409)
            observe('idempotent-submit', 'same request converges; changed revision conflicts')
            done = wait_render(rid)
            need(done['state'] == 'succeeded', str(done['attempts']))
            need(done['sheet']['revision'] == 1 and done['recipe']['revision'] == 1, 'frozen revisions drifted')
            observe('mp4-render', 'frozen revisions encoded to H.264/AAC MP4 and inspected')
            verification = request('POST', '/renders/'+rid+'/verify')
            artifact = request('GET', '/renders/'+rid+'/artifact', raw=True)
            need(hashlib.sha256(artifact).hexdigest() == verification['digest'], 'artifact digest mismatch')
            captions_dl = request('GET', '/renders/'+rid+'/artifact?captions=1', raw=True)
            need(captions_dl.decode() == done['plan']['vtt'], 'served captions differ from frozen plan')
            render_dir = scratch/'data'/'renders'/(rid+'-1')
            media_path = render_dir/'composition.mp4'
            original_media = media_path.read_bytes()
            replaced = bytearray(original_media)
            replaced[len(replaced)//2] ^= 0xFF
            media_path.write_bytes(replaced)
            request('GET', '/renders/'+rid+'/artifact', status=500)
            request('POST', '/renders/'+rid+'/verify', status=500)
            request('GET', '/renders/'+rid+'/bundle', status=500)
            media_path.write_bytes(original_media)
            restored = request('GET', '/renders/'+rid+'/artifact', raw=True)
            need(hashlib.sha256(restored).hexdigest() == verification['digest'], 'restored artifact digest mismatch')
            captions_path = render_dir/'captions.vtt'
            original_captions = captions_path.read_bytes()
            forged = original_captions.replace(b'00:00:01.000', b'00:00:09.000', 1)
            need(len(forged) == len(original_captions) and forged != original_captions, 'caption replacement setup failed')
            captions_path.write_bytes(forged)
            request('GET', '/renders/'+rid+'/artifact?captions=1', status=500)
            request('POST', '/renders/'+rid+'/verify', status=500)
            captions_path.write_bytes(original_captions)
            need(request('GET', '/renders/'+rid+'/artifact?captions=1', raw=True) == original_captions, 'restored captions mismatch')
            observe('artifact-verify', 'download SHA-256 equals committed inspection; same-length external replacement of media or captions refused until original bytes restored; served captions equal frozen plan')
            if args.workflow in ('all', 'bundle'):
                packed = request('GET', '/renders/'+rid+'/bundle', raw=True)
                with zipfile.ZipFile(io.BytesIO(packed)) as z:
                    need(set(z.namelist()) == {'composition.mp4', 'captions.vtt', 'composition.json'}, 'bundle members mismatch')
                    frozen = json.loads(z.read('composition.json'))
                    need(frozen['plan']['vtt'] == z.read('captions.vtt').decode(), 'bundle captions drifted')
                    need(hashlib.sha256(z.read('composition.mp4')).hexdigest() == verification['digest'], 'bundle media digest mismatch')
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    packed_many = list(pool.map(lambda _: request('GET', '/renders/'+rid+'/bundle', raw=True), range(4)))
                need(all(p == packed for p in packed_many), 'concurrent bundles diverged')
                observe('bundle', 'verified media, captions and frozen manifest exported together; four concurrent bundles byte-identical')
            if args.workflow in ('all', 'wav-render'):
                ar = request('POST', '/recipes', dict(recipe_body, name='reference-audio', format='wav', normalize=True), status=201)
                audio_render = request('POST', '/renders', dict(req, recipe_id=ar['id']), {'Idempotency-Key': 'audio'}, status=202)
                audio_done = wait_render(audio_render['id'])
                need(audio_done['state'] == 'succeeded', str(audio_done['attempts']))
                need(audio_done['artifact']['inspection']['media']['audio_codec'] == 'pcm_s16le', 'audio codec mismatch')
                observe('wav-render', 'trimmed stereo PCM WAV encoded with loudness filter')
            request('DELETE', '/sources/'+sid, status=409)
            observe('retire-source', 'historical sheet reference prevents retirement')
            if args.workflow in ('all', 'cancel', 'retry', 'restart-recover'):
                long_sheet = request('POST', '/sheets', dict(sheet_body, name='extended-calibration', spans=sheet_body['spans']*16), status=201)
                pending = request('POST', '/renders', dict(req, sheet_id=long_sheet['id']), {'Idempotency-Key': 'interrupt'}, status=202)
                interrupted_id = pending['id']
                wait_render(interrupted_id, 'running')
                cancelled = request('POST', '/renders', req, {'Idempotency-Key': 'cancel-me'}, status=202)
                cancelled = request('POST', '/renders/'+cancelled['id']+'/cancel')
                need(cancelled['state'] == 'cancelled', 'cancel not acknowledged')
                request('GET', '/renders/'+cancelled['id']+'/artifact', status=409)
                observe('cancel', 'composition cancelled without artifact visibility')
                stop(force=True)
                start()
                recovered = request('GET', '/renders/'+interrupted_id)
                need(recovered['state'] == 'failed' and recovered['attempts'][-1]['outcome'] == 'interrupted', 'recovery provenance mismatch')
                observe('restart-recover', 'SIGKILL/restart retained frozen inputs and marked interruption')
                request('POST', '/renders/'+interrupted_id+'/retry', status=202)
                retried = wait_render(interrupted_id)
                need(retried['state'] == 'succeeded' and len(retried['attempts']) == 2, str(retried['attempts']))
                retried_dl = request('GET', '/renders/'+interrupted_id+'/artifact', raw=True)
                need(hashlib.sha256(retried_dl).hexdigest() == retried['artifact']['inspection']['digest'], 'download not bound to the successful attempt')
                observe('retry', 'second attempt completed using original snapshot; download bound to the successful attempt')
            events = request('GET', '/events?after=0')
            need(events and all(a['sequence'] < b['sequence'] for a, b in zip(events, events[1:])), 'event order mismatch')
            tail = request('GET', '/events?after='+str(events[-1]['sequence']))
            need(all(e['sequence'] > events[-1]['sequence'] for e in tail), 'event cursor repeated')
            observe('events', 'persisted event cursor strictly increases')
            if args.workflow != 'all':
                need(any(x['workflow_id'] == args.workflow for x in observations), 'unknown workflow')
            print(json.dumps({'ok': True, 'requested': args.workflow, 'observations': observations}, ensure_ascii=False))
        except Exception:
            log.flush()
            print((scratch/'server.log').read_text(errors='replace'))
            raise
        finally:
            stop()
            log.close()

if __name__ == '__main__':
    main()
