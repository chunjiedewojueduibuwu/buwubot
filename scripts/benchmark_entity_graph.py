"""Synthetic local entity-index benchmark; no live files, models or network."""
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
from buwubot.memory import MemoryService, content_key
from buwubot.memory_graph import MemoryGraph
from buwubot.store import Store


def measure(operation, rounds=50):
    operation()
    times = []
    for _ in range(rounds):
        start = time.perf_counter()
        result = operation()
        times.append((time.perf_counter() - start) * 1000)
    tracemalloc.start()
    operation()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {'median_ms': statistics.median(times), 'p95_ms': sorted(times)[int(.95*(rounds-1))],
            'returned': len(result), 'python_peak_bytes': peak,
            'max_entity_path_hops': max((len(item.get('entity_path', {}).get('steps', [])) for item in result), default=0)}


def benchmark(size):
    with tempfile.TemporaryDirectory(prefix='buwubot-entity-benchmark-') as directory:
        path = Path(directory) / 'synthetic.db'
        store = Store(path)
        cfg = Settings(group_id='123', self_id='11', embedding_provider='none')
        store.save_settings(cfg, store.settings()[1])
        memory = MemoryService(store, cfg)
        graph = MemoryGraph(memory)
        try:
            # Bulk synthetic fixture loading measures lookup, not AI extraction or write throughput.
            with store.transaction() as db:
                for n in range(size):
                    text = f'entity{n:06} alias{n:06}'
                    db.execute("""INSERT INTO memories(id,group_id,subject_id,content,content_key,tier,tags,
                        confidence,source_ids,event_time,created_at,updated_at,revision,epoch,deleted,persona_id)
                        VALUES(?,?,?,?,?,'important','[]',.9,'["synthetic"]',?,1,1,1,1,0,NULL)""",
                        (f'm{n:06}', '123', '22', text, content_key('123','22',text), n+1))
            store.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            baseline = path.stat().st_size
            with store.transaction() as db:
                db.executemany('INSERT INTO memory_graph_entities VALUES(?,?,?,?,?)',
                    [(f'e{n:06}', '123', cfg.persona_id, 'topic', f'entity{n:06}') for n in range(size)])
                db.executemany('INSERT INTO memory_graph_mentions VALUES(?,?,?,?,?,?,?)',
                    [(f'e{n:06}', f'm{n:06}', 1, f'{prefix}{n:06}', f'{prefix}{n:06}', int(prefix=='entity'), .9)
                     for n in range(size) for prefix in ('entity', 'alias')])
                db.executemany('INSERT INTO memory_graph_edges VALUES(?,?,?,?,?,?,?,?,?,?)',
                    [(f'r{n:06}', '123', cfg.persona_id, f'e{n:06}', f'e{n+1:06}', 'relates_to',
                      'synthetic association', .9, n+2, json.dumps([[f'm{n:06}',1],[f'm{n+1:06}',1]]))
                     for n in range(size-1)])
            store.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            graph_bytes = path.stat().st_size
            operations = {'exact_alias': lambda: graph.lookup('alias000000'),
                'timeline': lambda: graph.query({'mode':'timeline','entities':['alias000000']}),
                'three_hop_network': lambda: graph.query({'mode':'network','entities':['alias000000','alias000003']})}
            results = {name: measure(operation) for name, operation in operations.items()}
            rss = psutil.Process().memory_info().rss
            return {'size':size,'entities':size,'name_alias_rows':size*2,'typed_edges':size-1,
                'baseline_database_bytes':baseline,'with_graph_database_bytes':graph_bytes,
                'entity_graph_storage_delta_bytes':graph_bytes-baseline,'process_rss_bytes':rss,
                'operations':results,'embedding':'disabled','models_and_network':'not used',
                'notes':['Synthetic chain, not general semantic accuracy or dense-graph coverage.',
                    '50 warm queries; excludes extraction, model calls and index write throughput.',
                    'RSS includes Python, construction, FTS and SQLite caches; not isolated graph RAM.',
                    'Bulk loading synthetic fixtures bypasses model/review gates; live data is never read.']}
        finally:
            store.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--size',type=int,choices=[100,10000],required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    report=benchmark(args.size)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True))
