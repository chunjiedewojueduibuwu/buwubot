"""Build the bundled skills without caches, credentials or machine configuration."""

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def package_skills(root: Path, output: Path) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    packages = []
    for source in sorted((root / 'skill_packages').iterdir()):
        if not source.is_dir() or source.is_symlink():
            continue
        manifest = json.loads((source / 'plugin.json').read_text(encoding='utf-8'))
        # Bundled skills have flat source trees; additions must be reviewed here.
        names = ['plugin.json', 'main.py', 'README.md']
        if source.name == 'bilibili_video':
            names.append('asr_worker.py')
        target = output / f'{source.name}-{manifest["version"]}.zip'
        with ZipFile(target, 'w', compression=ZIP_DEFLATED) as archive:
            for name in sorted(names):
                file = source / name
                if file.is_symlink() or not file.is_file():
                    raise ValueError(f'Bundled source missing or linked: {file}')
                info = ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                archive.writestr(info, file.read_bytes())
        packages.append(target)
    return packages


if __name__ == '__main__':
    project = Path(__file__).resolve().parents[1]
    for package in package_skills(project, project / 'dist'):
        print(package.name)
