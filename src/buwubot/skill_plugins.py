"""Local skill packages: static installation, durable settings, isolated invocation."""

import asyncio
import base64
import binascii
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .coordinator import require_coordinator
from .process_tree import contain_process
from .store import Conflict
from .tools import ToolRegistry, ToolResult

RESERVED = {'con', 'prn', 'aux', 'nul', *(f'com{n}' for n in range(1,10)),
            *(f'lpt{n}' for n in range(1,10))}


class Setting(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(pattern=r'^[a-z][a-z0-9_]{0,63}$')
    label: str = Field(min_length=1,max_length=80)
    type: Literal['string','integer','boolean']
    default: str | int | bool | None = None
    secret: bool = False

    def value(self, value):
        expected = {'string':str,'integer':int,'boolean':bool}[self.type]
        if type(value) is not expected or (isinstance(value,str) and len(value) > 6000):
            raise ValueError('invalid plugin setting value')
        return value

    @model_validator(mode='after')
    def check_default(self):
        if self.secret and (self.type != 'string' or self.default not in (None,'')):
            raise ValueError('secret settings cannot contain packaged defaults')
        if self.default is None:
            self.default = {'string':'','integer':0,'boolean':False}[self.type]
        self.value(self.default)
        return self


class SkillTool(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(pattern=r'^[a-z][a-z0-9_]{0,63}$')
    description: str = Field(min_length=1,max_length=1000)
    input: dict
    continuation: bool = Field(default=False, strict=True)


class Manifest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    format_version: Literal[1]
    id: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,39}$')
    name: str = Field(min_length=1,max_length=80)
    version: str = Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9._+-]{0,63}$')
    description: str = Field(min_length=1,max_length=1000)
    entrypoint: str = Field(pattern=r'^[a-zA-Z_][a-zA-Z0-9_]*\.py$')
    tools: list[SkillTool] = Field(min_length=1,max_length=16)
    settings: list[Setting] = Field(default_factory=list,max_length=32)

    @model_validator(mode='after')
    def check_names(self):
        if self.id in RESERVED:
            raise ValueError('reserved plugin ID')
        for values in (self.tools,self.settings):
            if len({item.name for item in values}) != len(values):
                raise ValueError('duplicate plugin field name')
        # Validate tool declarations without executing supplied code.
        probe = ToolRegistry()
        for tool in self.tools:
            probe.register(tool.name,lambda _:None,description=tool.description,input_schema=tool.input,
                           continuation=tool.continuation)
        return self


class SkillPlugins:
    def __init__(self,store,tools: ToolRegistry,directory: Path, *, media=None):
        self.store,self.tools = store,tools
        self.media = media
        self.directory = Path(directory).resolve()
        self.objects = self.directory / 'objects'
        self.objects.mkdir(parents=True,exist_ok=True)
        self.active_calls = {}
        self.call_timeout = 25
        self._owned,self._errors,self._retired = {},{},set()
        with store.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS skill_plugins('
                'id TEXT PRIMARY KEY,manifest TEXT NOT NULL,digest TEXT NOT NULL,'
                'config TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 1)')
        for row in store.rows('SELECT * FROM skill_plugins'):
            self._attach(dict(row))

    def _record(self,pid,expected_revision=None):
        rows = self.store.rows('SELECT * FROM skill_plugins WHERE id=?',(pid,))
        if not rows:
            raise KeyError(pid)
        row = dict(rows[0])
        if expected_revision is not None and row['revision'] != expected_revision:
            raise Conflict('plugin configuration changed; refresh before editing')
        return row

    def _public(self,row):
        manifest = Manifest.model_validate_json(row['manifest'])
        config = json.loads(row['config'])
        secrets = {field.name for field in manifest.settings if field.secret}
        return manifest.model_dump() | {'enabled':bool(row['enabled']), 'revision':row['revision'],
            'config':{k:v for k,v in config.items() if k not in secrets},
            'secret_fields_set':sorted(k for k in secrets if config.get(k)),
            'error':self._errors.get(row['id'])}

    def list(self):
        return [self._public(dict(row)) for row in self.store.rows('SELECT * FROM skill_plugins ORDER BY id')]

    def _package_path(self,digest):
        if not re.fullmatch(r'[0-9a-f]{64}',digest):
            raise ValueError('invalid package digest')
        path = (self.objects / digest).resolve()
        if path.parent != self.objects.resolve():
            raise ValueError('package path escapes the plugin directory')
        return path

    def _safe_remove(self,path):
        path = path.resolve()
        if not ((path.parent == self.objects.resolve() and re.fullmatch(r'[0-9a-f]{64}',path.name)) or
                (path.parent == self.directory and path.name.startswith('.stage-'))):
            raise ValueError('refusing deletion outside the plugin workspace')
        if path.exists():
            shutil.rmtree(path)

    def _detach(self,pid):
        for name in self._owned.pop(pid,[]):
            self.tools.unregister(name)

    def _attach(self,row):
        self._detach(row['id'])
        self._errors.pop(row['id'],None)
        try:
            manifest = Manifest.model_validate_json(row['manifest'])
            if any(tool.name == 'qq_operation' for tool in manifest.tools):
                raise ValueError('tool name belongs to the QQ channel')
            folder = self._package_path(row['digest'])
            if not (folder / manifest.entrypoint).is_file():
                raise ValueError('missing plugin entrypoint')
            config = json.loads(row['config'])
            self._owned[row['id']] = []
            for tool in manifest.tools:
                handler = partial(self._run,row['digest'],manifest,config,tool.name)
                self.tools.register(tool.name,handler,description=tool.description,input_schema=tool.input,
                                    continuation=tool.continuation)
                self._owned[row['id']].append(tool.name)
                if not row['enabled']:
                    self.tools.disable(tool.name)
        except (ValueError,OSError,KeyError):
            self._detach(row['id'])
            self._errors[row['id']] = '插件文件或声明无法载入，请检查后重新安装'

    def install(self,package: bytes):
        require_coordinator()
        if len(package) > 8 * 1024 * 1024:
            raise ValueError('plugin package exceeds 8 MiB')
        try:
            archive = zipfile.ZipFile(io.BytesIO(package))
        except zipfile.BadZipFile:
            raise ValueError('invalid plugin ZIP package') from None
        with archive:
            entries = archive.infolist()
            if len(entries) > 128 or sum(entry.file_size for entry in entries) > 16 * 1024 * 1024:
                raise ValueError('expanded plugin package exceeds limits')
            seen = set()
            for entry in entries:
                name = entry.filename.rstrip('/')
                parts = name.split('/')
                if (not name or chr(92) in name or ':' in name or name.startswith('/') or
                        any(p in {'','.','..'} or p.endswith(('.',' ')) or p.split('.')[0].casefold() in RESERVED
                            for p in parts) or name.casefold() in seen or
                        stat.S_IFMT(entry.external_attr >> 16) not in {0,stat.S_IFREG,stat.S_IFDIR}):
                    raise ValueError('unsafe or duplicate plugin archive path')
                seen.add(name.casefold())
            if 'plugin.json' not in archive.namelist() or archive.getinfo('plugin.json').file_size > 65536:
                raise ValueError('missing or oversized plugin manifest')
            manifest = Manifest.model_validate_json(archive.read('plugin.json'))
            if any(tool.name == 'qq_operation' for tool in manifest.tools):
                raise ValueError('tool name belongs to the QQ channel')
            if manifest.entrypoint not in archive.namelist():
                raise ValueError('plugin entrypoint is missing')
            if self.store.rows('SELECT id FROM skill_plugins WHERE id=?',(manifest.id,)):
                raise Conflict('plugin already installed; remove it before installing another version')
            existing = {tool['name'] for tool in self.tools.definitions()}
            for installed in self.store.rows('SELECT manifest FROM skill_plugins'):
                existing.update(tool.name for tool in Manifest.model_validate_json(installed['manifest']).tools)
            if any(tool.name in existing for tool in manifest.tools):
                raise ValueError('plugin tool name conflicts with an installed tool')
            digest = hashlib.sha256(package).hexdigest()
            folder = self._package_path(digest)
            stage = Path(tempfile.mkdtemp(prefix='.stage-',dir=self.directory))
            created = False
            try:
                for entry in entries:
                    target = stage.joinpath(*entry.filename.rstrip('/').split('/'))
                    if entry.is_dir():
                        target.mkdir(parents=True,exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True,exist_ok=True)
                        target.write_bytes(archive.read(entry))
                if not folder.exists():
                    stage.replace(folder)
                    created = True
                config = {field.name:field.default for field in manifest.settings}
                with self.store.transaction() as db:
                    db.execute('INSERT INTO skill_plugins(id,manifest,digest,config) VALUES(?,?,?,?)',
                        (manifest.id,manifest.model_dump_json(),digest,json.dumps(config,ensure_ascii=False)))
            except BaseException:
                if created:
                    self._safe_remove(folder)
                raise
            finally:
                self._safe_remove(stage)
        self._attach(self._record(manifest.id))
        return self._public(self._record(manifest.id))

    def configure(self,pid,changes: dict,*,expected_revision: int):
        require_coordinator()
        row = self._record(pid,expected_revision)
        fields = {field.name:field for field in Manifest.model_validate_json(row['manifest']).settings}
        if set(changes) - fields.keys():
            raise ValueError('unknown plugin setting')
        config = json.loads(row['config']) | {key:fields[key].value(value) for key,value in changes.items()}
        with self.store.transaction() as db:
            db.execute('UPDATE skill_plugins SET config=?,revision=revision+1 WHERE id=?',
                (json.dumps(config,ensure_ascii=False),pid))
        self._attach(self._record(pid))
        return self._public(self._record(pid))

    def set_enabled(self,pid,enabled: bool,*,expected_revision: int):
        require_coordinator()
        self._record(pid,expected_revision)
        with self.store.transaction() as db:
            db.execute('UPDATE skill_plugins SET enabled=?,revision=revision+1 WHERE id=?',(int(enabled),pid))
        self._attach(self._record(pid))
        if enabled and pid not in self._errors:
            with self.store.transaction() as db:
                for name in self._owned[pid]:
                    db.execute("UPDATE jobs SET state='pending',next_attempt=0,last_error=NULL "
                        "WHERE kind='tool' AND state='needs_attention' AND last_error=? "
                        "AND json_extract(payload,'$.tool_name')=?",
                        ('工具处理器未加载，请检查配置',name))
        return self._public(self._record(pid))

    def uninstall(self,pid,*,expected_revision: int):
        require_coordinator()
        row = self._record(pid,expected_revision)
        self._detach(pid)
        with self.store.transaction() as db:
            db.execute('DELETE FROM skill_plugins WHERE id=?',(pid,))
        self._errors.pop(pid,None)
        if self.active_calls.get(row['digest']):
            self._retired.add(row['digest'])
        else:
            self._safe_remove(self._package_path(row['digest']))
        # Per-plugin operation data is retained for reinstall/resume, never mixed with persona memory.

    async def _run(self,digest,manifest,config,name,arguments):
        request = json.dumps({'protocol_version':1,'tool':name,'arguments':arguments,'config':config},
                             ensure_ascii=False,allow_nan=False).encode('utf-8')
        if len(request) > 128 * 1024:
            raise ValueError('plugin request exceeds 128 KiB')
        folder = self._package_path(digest)
        work = self.directory / 'data' / manifest.id
        work.mkdir(parents=True,exist_ok=True)
        env = {key:value for key,value in os.environ.items() if key.upper() in
               {'PATH','SYSTEMROOT','WINDIR','TEMP','TMP'}}
        launcher = ("import sys,runpy;assert sys.stdin.buffer.read(1)==b'\\n';"
                    "sys.path.insert(0,sys.argv[3]);sys.path.insert(0,sys.argv[1]);"
                    "runpy.run_path(sys.argv[2],run_name='__main__')")
        process = None
        close_tree = None
        self.active_calls[digest] = self.active_calls.get(digest,0) + 1
        try:
            async with asyncio.timeout(self.call_timeout):
                process = await asyncio.create_subprocess_exec(sys.executable,'-I','-X','utf8','-c',launcher,
                    str(folder),str(folder / manifest.entrypoint),str(Path(__file__).resolve().parent.parent),
                    cwd=work,env=env,
                    stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=os.name != 'nt',
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                close_tree = contain_process(process.pid)
                process.stdin.write(b'\n' + request)
                await process.stdin.drain()
                process.stdin.close()
                output = bytearray()
                while chunk := await process.stdout.read(8192):
                    output.extend(chunk)
                    if len(output) > 128 * 1024:
                        raise ValueError('plugin output exceeds 128 KiB')
                await process.wait()
                if process.returncode:
                    raise ValueError('plugin process failed')
                result = json.loads(output)
                declaration = next(tool for tool in manifest.tools if tool.name == name)
                if (isinstance(result,dict) and result.get('next_arguments') is not None
                        and not declaration.continuation):
                    raise ValueError('tool has no continuation permission')
                return self._import_assets(result)
        finally:
            if close_tree:
                close_tree()
            if process and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
            self.active_calls[digest] -= 1
            if not self.active_calls[digest]:
                del self.active_calls[digest]
                if digest in self._retired:
                    self._retired.remove(digest)
                    if not self.store.rows('SELECT id FROM skill_plugins WHERE digest=?',(digest,)):
                        self._safe_remove(folder)

    def _import_assets(self, result):
        if not isinstance(result, dict):
            raise TypeError('plugin result must be an object')
        result = dict(result)
        assets = result.pop('assets', [])
        if not isinstance(assets, list) or len(assets) > 10:
            raise ValueError('invalid plugin assets')
        if assets and (self.media is None or result.get('state') != 'succeeded'):
            raise ValueError('image registration unavailable or unfinished')
        decoded = {}
        for asset in assets:
            if (not isinstance(asset, dict) or set(asset) != {'hash', 'base64'}
                    or not isinstance(asset['hash'], str) or not isinstance(asset['base64'], str)
                    or len(asset['base64']) > 120000):
                raise ValueError('invalid inline image')
            try:
                content = base64.b64decode(asset['base64'], validate=True)
            except (ValueError, binascii.Error):
                raise ValueError('invalid image encoding') from None
            if hashlib.sha256(content).hexdigest() != asset['hash']:
                raise ValueError('image hash mismatch')
            self.media.inspect(content)
            decoded[asset['hash']] = content
        images = result.get('evidence_images', [])
        if not isinstance(images, list):
            raise TypeError('invalid image references')
        if any(not isinstance(image,str) for image in images):
            raise ValueError('invalid image reference')
        result['evidence_images'] = list(dict.fromkeys(images + list(decoded)))
        validated = ToolResult.model_validate(result).model_dump()
        # Validate the complete payload before registering any new image.
        for content in decoded.values():
            self.media.save(content)
        return validated
