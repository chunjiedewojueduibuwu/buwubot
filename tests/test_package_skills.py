import hashlib
import importlib.util
from pathlib import Path
from zipfile import ZipFile

from buwubot.skill_plugins import Manifest


def test_bundled_skill_build_is_repeatable_and_excludes_local_files(tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('package_skills', root / 'scripts/package_skills.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    packages = module.package_skills(root, tmp_path)
    assert len(packages) == 2
    before = {p.name: hashlib.sha256(p.read_bytes()).digest() for p in packages}
    for package in packages:
        with ZipFile(package) as archive:
            manifest = Manifest.model_validate_json(archive.read('plugin.json'))
            expected = {'plugin.json', 'main.py', 'README.md'}
            if manifest.id == 'bilibili_video':
                expected.add('asr_worker.py')
            assert set(archive.namelist()) == expected
            assert manifest.entrypoint in expected
    again = module.package_skills(root, tmp_path)
    assert before == {p.name: hashlib.sha256(p.read_bytes()).digest() for p in again}
