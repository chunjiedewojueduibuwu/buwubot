"""Local-only video/ASR load probe. No QQ, network, model API or production writes."""

import argparse
import asyncio
import json
import os
import shutil
import time
from pathlib import Path

import psutil

from buwubot.stickers import MediaStore
from buwubot.store import Store
from buwubot.video_media import TimelineSource, run_media


async def measure(name, operation):
    parent = psutil.Process()
    before = parent.memory_info().rss
    record = {'phase':name, 'parent_before_mib':round(before / 2**20, 2),
              'peak_child_rss_mib':0, 'peak_total_rss_mib':before / 2**20, 'peak_child_threads':0}
    cpu, children = {}, set()
    finished = asyncio.Event()
    async def sample():
        while not finished.is_set():
            rss, threads = 0, 0
            for process in parent.children(recursive=True):
                try:
                    children.add(process.pid)
                    rss += process.memory_info().rss
                    threads += process.num_threads()
                    times = process.cpu_times()
                    cpu[process.pid] = max(cpu.get(process.pid, 0), times.user + times.system)
                except psutil.Error:
                    pass
            record['peak_child_rss_mib'] = max(record['peak_child_rss_mib'], rss / 2**20)
            record['peak_total_rss_mib'] = max(record['peak_total_rss_mib'],
                                              (rss + parent.memory_info().rss) / 2**20)
            record['peak_child_threads'] = max(record['peak_child_threads'], threads)
            await asyncio.sleep(.025)
    monitor = asyncio.create_task(sample())
    start = time.perf_counter()
    try:
        result = await operation()
        record['outcome'] = 'success'
    except Exception as exc:  # noqa: BLE001 -- keep sanitized local failure evidence
        result = None
        record.update(outcome='failed', error_type=type(exc).__name__)
    finally:
        record['seconds'] = round(time.perf_counter() - start, 3)
        finished.set()
        await monitor
    record.update(sampled_child_cpu_seconds=round(sum(cpu.values()), 3), child_pids_seen=len(children),
                  children_remaining=len(parent.children(recursive=True)))
    for key in ('peak_child_rss_mib', 'peak_total_rss_mib'):
        record[key] = round(record[key], 2)
    return record, result


async def main(args):
    project = args.source.resolve()
    output = args.output.resolve()
    root = Path(__file__).resolve().parents[1]
    if not output.is_relative_to((root / '.tools').resolve()) or output == root / '.tools':
        raise ValueError('use a dedicated .tools directory')
    output.mkdir(parents=True, exist_ok=True)
    python = project / 'runtime/video-asr/Scripts/python.exe'
    model = project / 'runtime/video-asr/models/tiny'
    worker = root / 'skill_packages/bilibili_video/asr_worker.py'
    fixture = project / '.tools/video-audio-sample-16k.wav'
    ffmpeg = shutil.which('ffmpeg')
    report = {'scope':'isolated local decode/ASR; no network, QQ or actual model API calls',
        'cpu_logical_count':os.cpu_count(), 'sample_interval_seconds':.025,
        'limits':['RSS sums include shared pages; sampling can miss short peaks',
                  'CPU time is last observed child time, not a complete profiler',
                  'first ASR process is cold start; Windows disk cache is not flushed',
                  'synthetic 24-second video with repeated existing audio fixture; not gaming benchmark'],
        'phases':[]}
    target = output / 'report.json'
    def checkpoint():
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    async def phase(name, operation):
        record, result = await measure(name, operation)
        report['phases'].append(record)
        checkpoint()
        print(json.dumps(record), flush=True)
        if record['outcome'] != 'success':
            raise RuntimeError('probe failed; checkpoint preserved')
        return result
    if not all(path.is_file() for path in (python, model/'model.bin', worker, fixture)) or not ffmpeg:
        report['unavailable'] = 'prepared local speech runtime or fixture missing'
        checkpoint()
        return
    store = Store(output / 'probe.db')
    media = MediaStore(store, output / 'media')
    folder = output / 'segments'
    folder.mkdir(exist_ok=True)
    source = TimelineSource(folder, media=media, asr_python=str(python), asr_model_dir=str(model),
                            worker=str(worker))
    report['weight_mib'] = round((model/'model.bin').stat().st_size / 2**20, 2)
    video = output / 'synthetic.mp4'
    try:
        await phase('generate_fixture', lambda:run_media(ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
            '-nostdin', '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=24',
            '-stream_loop', '-1', '-i', fixture, '-t', '24', '-c:v', 'libx264',
            '-preset', 'ultrafast', '-threads', '1', '-pix_fmt', 'yuv420p', '-c:a', 'aac', video))
        for start in (0, 12):
            frames, audio = await phase(f'decode_{start}_{start+12}',
                                       lambda start=start:source._decode(video, start, start+12))
            speech = await phase(f'asr_{start}_{start+12}', lambda audio=audio:source._transcribe(audio))
            report.setdefault('coverage', []).append({'start':start, 'end':start+12,
                'frames':len(frames), 'audio_status':speech['audio_status'], 'speech_spans':len(speech['speech'])})
            checkpoint()
            await phase(f'decode_cache_{start}', lambda start=start:source._decode(video, start, start+12))
            await phase(f'asr_cache_{start}', lambda audio=audio:source._transcribe(audio))
        async def interrupt():
            task = asyncio.create_task(run_media(python, '-I', '-X', 'utf8', worker, audio, model))
            try:
                async with asyncio.timeout(3):
                    while True:
                        if task.done():
                            raise RuntimeError('ASR finished before interruption could be measured')
                        rss = 0
                        for child in psutil.Process().children(recursive=True):
                            try:
                                rss += child.memory_info().rss
                            except psutil.Error:
                                pass
                        if rss >= 200 * 2**20:
                            report['cancel_observed_child_rss_mib'] = round(rss / 2**20, 2)
                            break
                        await asyncio.sleep(.01)
                await asyncio.sleep(.05)
                start = time.perf_counter()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                report['cancel_release_seconds'] = round(time.perf_counter()-start, 3)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        await phase('interrupt_asr', interrupt)
    finally:
        await source.close()
        store.close()
        checkpoint()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
