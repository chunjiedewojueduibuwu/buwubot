"""Synthetic SQLite-only benchmark; never reads the live data directory or calls models."""

import argparse
import json
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from buwubot.config import Settings
from buwubot.consolidation import MemoryConsolidation
from buwubot.memory import MemoryService, content_key
from buwubot.store import Store


def measure(operation, rounds=50):
    operation()
    timings = []
    for _ in range(rounds):
        start = time.perf_counter()
        result = operation()
        timings.append((time.perf_counter() - start) * 1000)
    tracemalloc.start()
    operation()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {'median_ms': statistics.median(timings),
            'p95_ms': sorted(timings)[int(.95 * (len(timings) - 1))],
            'python_peak_bytes_one_query': peak, 'returned': len(result),
            'max_returned_path_hops': max((len(item.get('relation_recall', {}).get('steps', [])) for item in result), default=0),
            'includes_expected_linked_fact': any(item['id'] == 'm000001' for item in result)}


def benchmark(size):
    with tempfile.TemporaryDirectory(prefix='buwubot-relation-benchmark-') as directory:
        path = Path(directory) / 'benchmark.db'
        store = Store(path)
        cfg = Settings(group_id='123', self_id='11', embedding_provider='none')
        store.save_settings(cfg, store.settings()[1])
        memory = MemoryService(store, cfg)
        process = psutil.Process()
        try:
            with store.transaction() as db:
                for n in range(size):
                    mid = f'm{n:06}'
                    text = ('茶叶放在红盒子' if n == 0 else '红盒子位于书架顶层' if n == 1 else f'独立归档事项{n}')
                    db.execute('''INSERT INTO memories(id,group_id,subject_id,content,content_key,tier,tags,
                        confidence,source_ids,event_time,created_at,updated_at,revision,epoch,deleted,persona_id)
                        VALUES(?,?,?,?,?,'important','[]',.9,'["synthetic"]',NULL,1,1,1,1,0,NULL)''',
                        (mid, '123', '22', text, content_key('123', '22', text)))
            store.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            baseline_bytes = path.stat().st_size
            baseline_rss = process.memory_info().rss
            def lexical():
                return memory.search_text('茶叶', group_id='123')
            baseline = measure(lexical)
            with store.transaction() as db:
                edges = [(f'chain{n:06}', f'm{n:06}', f'm{n+1:06}') for n in range(size - 1)]
                edges.extend((f'hub{n:06}', 'm000000', f'm{n:06}') for n in range(2, min(size, 1002)))
                db.executemany('INSERT INTO memory_relations VALUES(?,?,?, ?,1,?,1,?,?,?,1)',
                    [(key, cfg.persona_id, '123', source, target, 'related', '合成位置关联', '["synthetic"]')
                     for key, source, target in edges])
            store.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            graph_bytes = path.stat().st_size
            relations = MemoryConsolidation(memory)
            def expanded(depth=1):
                seeds = lexical()
                return seeds + relations.neighbors([item['id'] for item in seeds], max_hops=depth)
            graphs = {str(depth): measure(lambda depth=depth: expanded(depth)) for depth in (1, 2, 3)}
            # Compare a genuine deep chain separately from the hub material-budget stress case.
            store.connection.execute("DELETE FROM memory_relations WHERE id LIKE 'hub%'")
            chains = {str(depth): measure(lambda depth=depth: expanded(depth)) for depth in (1, 2, 3)}
            info = process.memory_info()
            return {'size': size, 'relation_rows': len(edges), 'embedding': 'disabled',
                'baseline_database_bytes': baseline_bytes, 'with_relations_database_bytes': graph_bytes,
                'relation_storage_delta_bytes': graph_bytes - baseline_bytes,
                'baseline_process_rss_bytes': baseline_rss, 'after_process_rss_bytes': info.rss,
                'process_peak_rss_bytes': getattr(info, 'peak_wset', None),
                'lexical_only': baseline, 'lexical_plus_one_hop': graphs['1'],
                'hub_by_depth': graphs, 'chain_by_depth': chains,
                'notes': ['Synthetic one-question coverage probe, not a general recall accuracy score.',
                    'Latency excludes models/network; same-process warm 50-query median/p95.',
                    'RSS includes Python, database construction and SQLite caches; not isolated graph service RAM.',
                    'Hub edges stress bounded material expansion; no live bot data was read.']}
        finally:
            store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--size', type=int, choices=[100, 10000], required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = benchmark(args.size)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True))
