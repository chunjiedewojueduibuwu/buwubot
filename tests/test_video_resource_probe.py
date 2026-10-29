import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('measure_video_resources',
    Path(__file__).parents[1] / 'scripts/measure_video_resources.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


@pytest.mark.parametrize('failed', [False, True])
async def test_resource_probe_records_parent_for_fast_cache_and_sanitizes_errors(failed):
    async def operation():
        await asyncio.sleep(0)
        if failed:
            raise ValueError('sensitive local detail')
        return 42
    record, result = await probe.measure('synthetic-cache', operation)
    assert record['peak_total_rss_mib'] >= record['parent_before_mib'] - .01
    assert record['peak_child_rss_mib'] == 0 and record['children_remaining'] == 0
    assert record['outcome'] == ('failed' if failed else 'success')
    assert result == (None if failed else 42)
    assert 'sensitive local detail' not in json.dumps(record)
