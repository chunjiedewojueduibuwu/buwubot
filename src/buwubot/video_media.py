"""Sequential local decoding for the video skill, with bounded optional speech extraction."""

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageDraw

from .learning import LearningFrame, LearningSegment, LearningSource, SourceUnavailable

MAX_MEDIA_BYTES = 128 * 1024 * 1024
INTERVAL_SECONDS = 12


def clean_streams(root, current):
    rows = []
    for folder in root.iterdir():
        if folder.is_symlink() or not folder.is_dir() or not re.fullmatch('[a-f0-9]{64}',folder.name):
            continue
        files = [p for p in folder.iterdir() if p.name in {'source.mp4','source.part','metadata.json'}
                 and p.is_file() and not p.is_symlink()]
        rows.append((folder,files,sum(p.stat().st_size for p in files),folder.stat().st_mtime))
    total = sum(row[2] for row in rows)
    for folder,files,size,modified in sorted(rows,key=lambda row:row[3]):
        if folder == current:
            continue
        if time.time()-modified > 86400 or total > 2*MAX_MEDIA_BYTES:
            try:
                for path in files:
                    path.unlink()
                folder.rmdir()
                total -= size
            except OSError:
                pass  # An active decoder may still own this file on Windows.


async def run_media(*arguments, timeout=15):
    process = await asyncio.create_subprocess_exec(*map(str,arguments),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
    try:
        output,_ = await asyncio.wait_for(process.communicate(),timeout)
        if process.returncode or len(output) > 65536:
            raise OSError('视频解码或转写暂不可用，保留片段进度')
        return output
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


class TimelineSource(LearningSource):
    def __init__(self, folder, *, media, asr_python, asr_model_dir, worker, audio=True):
        super().__init__(media=media)
        self.folder = Path(folder)
        self.asr_python, self.asr_model_dir, self.worker = asr_python, asr_model_dir, worker
        self.audio = audio

    def _stream_folder(self, canonical, part):
        key = hashlib.sha256(f'{canonical}:{part}'.encode()).hexdigest()
        return self.folder.parent / 'streams' / key

    async def _playback(self, url, part, start_seconds):
        video_id = self.identity(url)
        if type(part) is not int or part < 1 or not math.isfinite(start_seconds) or start_seconds < 0:
            raise SourceUnavailable('片段时间或分 P 无效')
        canonical = f'https://www.bilibili.com/video/{video_id}/'
        folder = self._stream_folder(canonical,part)
        metadata,media = folder/'metadata.json',folder/'source.mp4'
        if (metadata.is_file() and media.is_file() and 0 < media.stat().st_size <= MAX_MEDIA_BYTES
                and metadata.stat().st_size <= 4096 and time.time()-media.stat().st_mtime < 86400):
            try:
                row = json.loads(metadata.read_text(encoding='utf-8'))
                duration = float(row['duration'])
                if (row['video_id'] == video_id and row['canonical'] == canonical and row['part'] == part
                        and math.isfinite(duration) and duration > start_seconds
                        and isinstance(row['title'],str) and isinstance(row['uploader'],str)):
                    return (video_id,canonical,{'title':row['title'],'owner':{'name':row['uploader']}},
                            duration,row['stream'])
            except (ValueError,TypeError,KeyError):
                pass
        return await super()._playback(url,part,start_seconds)

    async def _download(self, url, canonical, part):
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in (None,443) or not parsed.hostname.endswith('.bilivideo.com')):
            raise SourceUnavailable('不支持的公开声画资源地址')
        # Shared by successive intervals; no credentials or external filenames enter local paths.
        folder = self._stream_folder(canonical,part)
        folder.mkdir(parents=True,exist_ok=True)
        os.utime(folder,None)
        clean_streams(folder.parent,folder)
        path, partial = folder/'source.mp4', folder/'source.part'
        if (path.is_file() and 0 < path.stat().st_size <= MAX_MEDIA_BYTES
                and time.time()-path.stat().st_mtime < 86400):
            return path
        offset = partial.stat().st_size if partial.is_file() else 0
        headers = {'Referer':canonical,'User-Agent':'BuwuBot/0.1 public-learning'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
        async with self.http.stream('GET',url,headers=headers,follow_redirects=False) as response:
            if response.status_code not in (200,206):
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                raise SourceUnavailable('公开声画资源暂不可读，不跟随跳转或绕过登录')
            append = response.status_code == 206
            if append:
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',response.headers.get('content-range',''))
                if (not match or int(match[1]) != offset or not offset <= int(match[2]) < int(match[3])
                        or int(match[3]) > MAX_MEDIA_BYTES):
                    raise SourceUnavailable('播放资源分段范围无效或超过本地缓存上限')
            else:
                offset = 0
            size = offset
            with partial.open('ab' if append else 'wb') as stream:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_MEDIA_BYTES:
                        raise SourceUnavailable('公开视频超过128MiB本地缓存上限，未按看完处理')
                    stream.write(chunk)
            if not size or append and size != int(match[3]):
                raise OSError('播放资源尚未完整下载，保留已下载进度')
        os.replace(partial,path)
        return path

    async def _decode(self, path, start, end):
        ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        if not ffmpeg or not ffprobe:
            raise SourceUnavailable('视频读取需要可用的 FFmpeg 和 FFprobe')
        stamp = hashlib.sha256(f'{start}:{end}'.encode()).hexdigest()[:16]
        prefix = self.folder / stamp
        manifest, audio = self.folder/(stamp+'.decoded'), self.folder/(stamp+'.wav')
        count = math.ceil(end-start-1e-8)
        frames = [self.folder/(stamp+f'-{i:03d}.jpg') for i in range(1,count+1)]
        if manifest.is_file():
            try:
                decoded = json.loads(manifest.read_text())
                if all(p.is_file() for p in frames) and (not decoded['has_audio'] or audio.is_file()):
                    return frames, audio if decoded['has_audio'] else None
            except (ValueError,KeyError,TypeError):
                manifest.unlink(missing_ok=True)
        info = json.loads(await run_media(ffprobe,'-v','error','-protocol_whitelist','file',
            '-show_entries','stream=codec_type','-of','json',path))
        has_audio = any(stream.get('codec_type') == 'audio' for stream in info['streams'])
        args = [ffmpeg,'-y','-hide_banner','-loglevel','error','-nostdin','-threads','1',
                '-filter_threads','1','-protocol_whitelist','file','-ss',f'{start:.6f}','-i',path,
                '-map','0:v:0','-t',f'{end-start:.6f}','-vf',
                'fps=1:start_time=0:round=up,scale=384:216:force_original_aspect_ratio=decrease',
                '-frames:v',str(count),'-an','-q:v','3',str(prefix)+'-%03d.jpg']
        if has_audio and self.audio:
            args += ['-map','0:a:0','-t',f'{end-start:.6f}','-vn','-ac','1','-ar','16000',
                     '-c:a','pcm_s16le',audio]
        await run_media(*args)
        if not all(p.is_file() and p.stat().st_size > 0 for p in frames):
            raise OSError('解码未覆盖完整片段，保留进度等待恢复')
        manifest.write_text(json.dumps({'has_audio':has_audio and self.audio}))
        return frames, audio if has_audio and self.audio else None

    async def _transcribe(self, audio):
        result_path = audio.with_suffix('.speech')
        if result_path.is_file():
            return json.loads(result_path.read_text(encoding='utf-8'))
        if (not self.asr_python or not Path(self.asr_python).is_file()
                or not self.asr_model_dir or not (Path(self.asr_model_dir)/'model.bin').is_file()):
            raise SourceUnavailable('请在视频技能设置中配置已准备的本地语音转写环境与模型')
        result = json.loads(await run_media(self.asr_python,'-I','-X','utf8',self.worker,
                                            audio,self.asr_model_dir))
        if result.get('audio_status') not in ('transcribed','no_speech_detected') or not isinstance(result.get('speech'),list):
            raise OSError('语音转写未返回可确认的时间段，保留片段')
        temporary = result_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
        os.replace(temporary,result_path)
        return result

    def _sheets(self, paths, start):
        frames = []
        for offset in range(0,len(paths),6):
            sheet = Image.new('RGB',(1152,472),'#10151b')
            draw = ImageDraw.Draw(sheet)
            times = []
            for i,path in enumerate(paths[offset:offset+6]):
                with Image.open(path) as original:
                    image = original.convert('RGB')
                    image.thumbnail((384,216))
                x,y = (i%3)*384,(i//3)*236
                sheet.paste(image,(x,y+20))
                timestamp = start+offset+i
                draw.text((x+4,y+3),f'{timestamp:.2f}s',fill='white')
                times.append(timestamp)
            for width in (1152,960,768,576):
                scaled = sheet if width == sheet.width else sheet.resize(
                    (width,round(sheet.height*width/sheet.width)),Image.Resampling.LANCZOS)
                for quality in (70,55,40,25,15):
                    output = BytesIO()
                    scaled.save(output,'JPEG',quality=quality,optimize=True)
                    if len(output.getvalue()) <= 32000:
                        break
                if len(output.getvalue()) <= 32000:
                    break
            else:
                raise SourceUnavailable('视频画面超过单次资料上限，未按完成处理')
            asset = self.media.save(output.getvalue())
            frames.append(LearningFrame(timestamp=times[0],frame_times=times,asset_hash=asset['hash']))
        return frames

    async def read_segment(self, url, part, start_seconds, span_seconds):
        if (type(span_seconds) not in (int,float) or not math.isfinite(span_seconds)
                or not 0 < span_seconds <= 300):
            raise SourceUnavailable('片段时间无效')
        if self.media is None:
            raise SourceUnavailable('连续视频读取需要启用画面读取')
        video_id,canonical,video,duration,stream = await self._playback(url,part,start_seconds)
        end = min(start_seconds+span_seconds,start_seconds+INTERVAL_SECONDS,duration)
        path = await self._download(stream,canonical,part)
        cache = self._stream_folder(canonical,part)
        if (cache/'source.mp4').is_file():
            temporary = cache/'metadata.tmp'
            temporary.write_text(json.dumps({'video_id':video_id,'canonical':canonical,'part':part,
                'title':str(video['title'])[:300],'uploader':str(video.get('owner',{}).get('name',''))[:100],
                'duration':duration,'stream':stream},ensure_ascii=False),encoding='utf-8')
            os.replace(temporary,cache/'metadata.json')
        paths,audio = await self._decode(path,start_seconds,end)
        sound = await self._transcribe(audio) if audio else {
            'audio_status':'no_audio_track' if self.audio else 'not_read','speech':[]}
        # ASR timestamps are relative to the decoded interval, not the full video.
        speech = [row | {'start':row['start']+start_seconds,'end':row['end']+start_seconds}
                  for row in sound['speech']]
        captions = []
        try:
            async with asyncio.timeout(2):
                captions = (await self._caption_segment(url,part,start_seconds,end-start_seconds)).captions
        except (SourceUnavailable,OSError,httpx.HTTPError,TimeoutError):
            pass  # Optional public captions supplement actual decoded sound/pictures.
        return LearningSegment(url=canonical+(f'?p={part}' if part>1 else ''),video_id=video_id,
            title=str(video['title'])[:300],uploader=str(video.get('owner',{}).get('name',''))[:100],
            part=part,start_seconds=start_seconds,end_seconds=end,duration=duration,
            captions=captions,frames=self._sheets(paths,start_seconds),coverage='timeline_frames',
            visual_fps=1,audio_status=sound['audio_status'],speech=speech,
            audio_language=sound.get('language'),audio_language_probability=sound.get('language_probability'))
