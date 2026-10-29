"""Measure the optional CPU worker; downloading needs an explicit --download."""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import psutil

from buwubot.config import Settings
from buwubot.embedding import LocalEncoder


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--cache",type=Path,default=Path("data/models"))
    parser.add_argument("--download",action="store_true")
    args=parser.parse_args()
    settings=Settings(embedding_provider="local",embedding_download_allowed=args.download)
    baseline=psutil.Process().memory_info().rss
    encoder=LocalEncoder(settings,args.cache.resolve())
    started=time.perf_counter()
    try:
        encoder.start()
        process=psutil.Process(encoder.process.pid)
        load_seconds=time.perf_counter()-started
        samples=[]
        for text in ["我喜欢喝绿茶","我不喝咖啡","代码评审","今晚一起打游戏","这是转述，不是我的偏好"]*6:
            started=time.perf_counter()
            vector=encoder.embed(text)
            samples.append((time.perf_counter()-started)*1000)
            assert len(vector)==512 and abs(sum(x*x for x in vector)-1)<.001
        rss=process.memory_info().rss
        private=process.memory_full_info().uss
        child_pid=process.pid
        details=encoder.details
    finally:
        encoder.close()
    assert "torch" not in sys.modules,"CPU inference dependencies leaked into the parent process"
    files=list(args.cache.rglob("*"))
    report={"model":settings.local_embedding_model,"worker":details,"load_seconds":round(load_seconds,3),
        "samples":len(samples),"median_ms":round(statistics.median(samples),3),
        "p95_ms":round(sorted(samples)[int(len(samples)*.95)-1],3),
        "worker_rss_mib":round(rss/2**20,1),"worker_private_mib":round(private/2**20,1),
        "parent_rss_before_mib":round(baseline/2**20,1),
        "parent_rss_after_mib":round(psutil.Process().memory_info().rss/2**20,1),
        "worker_exited":not psutil.pid_exists(child_pid),
        "cached_weight_bytes":sum(file.stat().st_size for file in files if file.suffix in {".safetensors",".bin"}),
        "cache_bytes":sum(file.stat().st_size for file in files if file.is_file())}
    print(json.dumps(report,ensure_ascii=False))


if __name__=="__main__":
    main()
