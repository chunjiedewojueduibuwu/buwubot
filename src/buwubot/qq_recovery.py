"""Opt-in recovery of a manually installed local bridge; never enable global autoload."""

import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import psutil

from .channels import ChannelError


class RecoveryError(ChannelError):
    def __init__(self, message, *, needs_attention=False):
        super().__init__(message)
        self.needs_attention = needs_attention


class SnowLumaRecovery:
    def __init__(self, directory, *, http=None):
        self.directory = Path(directory)
        self.http = http
        self.clock = time.monotonic
        self._owned = None
        self._loaded_at = 0
        self._retried = False

    @staticmethod
    def _installation(cfg):
        folder = Path(cfg.snowluma_directory)
        try:
            if not folder.is_absolute() or not cfg.self_id or cfg.self_id == cfg.owner_id:
                raise ValueError
            folder = folder.resolve(strict=True)
            if not all((folder / name).is_file() for name in ('node.exe','index.mjs')):
                raise ValueError
            runtime = json.loads((folder / 'config/runtime.json').read_text(encoding='utf-8'))
            consent = json.loads((folder / 'config/consent.json').read_text(encoding='utf-8'))
            webui = json.loads((folder / 'config/webui.json').read_text(encoding='utf-8'))
            port = runtime['webuiPort']
            if (runtime.get('hookAutoLoad') is not False or runtime.get('webuiHost') != '127.0.0.1'
                    or type(port) is not int or not 1 <= port <= 65535
                    or runtime.get('webuiTls',{}).get('enabled',False)
                    or not consent.get('version') or not consent.get('acceptedAt')
                    or webui.get('mustChangePassword') is not False
                    or not cfg.snowluma_password.get_secret_value()):
                raise ValueError
        except (OSError,ValueError,KeyError,TypeError,AttributeError):
            raise RecoveryError('请核对已初始化的 SnowLuma 目录、机器人账号和控制台密码；'
                                '仅支持本机 HTTP 且全局自动加载关闭',needs_attention=True) from None
        return folder, port

    @staticmethod
    def _process_matches(process, folder):
        try:
            working = Path(process.cwd()).resolve()
            command = process.cmdline()
            environment = {k.upper():v for k,v in process.environ().items()}
            autoload = environment.get('SNOWLUMA_HOOK_AUTOLOAD','').strip().lower()
            return (Path(process.exe()).resolve() == folder / 'node.exe' and working == folder
                    and not environment.get('NODE_OPTIONS') and not environment.get('NODE_PATH')
                    and autoload in {'','false','0','off','no'} and len(command) == 2
                    and (Path(command[1]) if Path(command[1]).is_absolute()
                         else working / command[1]).resolve() == folder / 'index.mjs')
        except (psutil.Error,OSError,ValueError):
            return False

    def _listener_matches(self, folder, port):
        try:
            listeners = [c for c in psutil.net_connections(kind='tcp')
                         if c.status == psutil.CONN_LISTEN and c.laddr.port == port]
            return bool(listeners) and all(c.pid and c.laddr.ip == '127.0.0.1'
                and self._process_matches(psutil.Process(c.pid),folder) for c in listeners)
        except psutil.Error:
            raise RecoveryError('无法核对 SnowLuma 监听进程，请在控制台手动连接',
                                needs_attention=True) from None

    def _start(self, folder, port):
        try:
            if any(c.status == psutil.CONN_LISTEN and c.laddr.port == port
                   for c in psutil.net_connections(kind='tcp')):
                raise RecoveryError('SnowLuma 端口由其他进程占用，未发送控制台密码',
                                    needs_attention=True)
            if any(self._process_matches(p,folder) for p in psutil.process_iter()):
                return
            if os.name != 'nt':
                raise RecoveryError('此自动恢复入口仅支持当前 Windows 安装',needs_attention=True)
            environment = {k:v for k,v in os.environ.items()
                           if not k.upper().startswith('SNOWLUMA_')
                           and k.upper() not in {'NODE_OPTIONS','NODE_PATH'}}
            environment.update(SNOWLUMA_HOOK_AUTOLOAD='false',SNOWLUMA_WEBUI_HOST='127.0.0.1',
                               SNOWLUMA_WEBUI_PORT=str(port),SNOWLUMA_WEBUI_TLS='false')
            subprocess.Popen([str(folder / 'node.exe'),str(folder / 'index.mjs')],cwd=folder,
                env=environment,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW,close_fds=True)
        except (OSError,psutil.Error):
            raise RecoveryError('SnowLuma 暂时无法启动，请核对程序目录') from None

    @staticmethod
    async def _request(http, base, path, *, token=None, data=None):
        try:
            response = await http.request('POST' if data is not None else 'GET',base + path,
                json=data,headers={'x-snowluma-token':token} if token else {},follow_redirects=False)
            if response.status_code != 200:
                raise RecoveryError('SnowLuma 控制台请求未成功，请核对登录和服务状态',
                                    needs_attention=300 <= response.status_code < 500)
            if len(response.content) > 1024 * 1024:
                raise ValueError
            result = response.json()
            if not isinstance(result,dict):
                raise TypeError
            return result
        except (httpx.HTTPError,ValueError,TypeError):
            raise RecoveryError('SnowLuma 控制台暂不可用或返回格式不符') from None

    @staticmethod
    def _candidate(result, expected):
        rows = result.get('list')
        if not isinstance(rows,list) or len(rows) > 128 or any(not isinstance(r,dict) for r in rows):
            raise RecoveryError('SnowLuma 账号列表暂不可用')
        matching = [r for r in rows if isinstance(r.get('account'),dict)
                    and str(r['account'].get('uin','')) == expected]
        if (len(matching) != 1 or type(matching[0].get('pid')) is not int
                or not 0 < matching[0]['pid'] <= 4194304):
            raise RecoveryError('等待唯一且已确认的机器人 QQ 客户端，不加载其他账号')
        return matching[0]

    @classmethod
    def _select(cls, result, expected):
        selected = cls._candidate(result,expected)
        # HookSession uses string '0' before loading; account is the independent OS login probe.
        if str(selected.get('uin') or expected) not in {expected,'0'}:
            raise RecoveryError('QQ 会话身份与已确认账号不一致，等待核对')
        return selected

    @staticmethod
    def _invalid_session(row):
        value = row.get('uin')
        return (row.get('injected') is True and row.get('connected') is True
                and row.get('loggedIn') is False and isinstance(value,str)
                and value.isascii() and value.isdecimal() and 10 < len(value) <= 20)

    async def _retry_owned(self, http, base, token, snapshot, cfg, folder, port, allowed):
        if (not self._owned or self._retried or self.clock() - self._loaded_at > 90 or not allowed()):
            return False
        selected = self._candidate(snapshot,cfg.self_id)
        identity = self._qq_identity(selected['pid'])
        if self._owned != (folder,cfg.self_id,identity) or not self._invalid_session(selected):
            return False
        verified = self._candidate(await self._request(http,base,'/api/processes',token=token),cfg.self_id)
        if (not allowed() or not self._invalid_session(verified) or verified['pid'] != selected['pid']
                or verified['uin'] != selected['uin'] or self._qq_identity(verified['pid']) != identity
                or not self._listener_matches(folder,port) or self.clock() - self._loaded_at > 90):
            return False
        # Consume before a potentially ambiguous HTTP outcome; never create an unload/load loop.
        self._retried = True
        result = await self._request(http,base,f"/api/processes/{verified['pid']}/unload",token=token,data={})
        if result.get('success') is not True:
            raise RecoveryError('本次加载的 QQ 模块未确认重连，等待控制台核对')
        return True

    @staticmethod
    def _qq_identity(pid):
        try:
            process = psutil.Process(pid)
            executable = Path(process.exe()).resolve()
            if executable.name.lower() != 'qq.exe':
                raise RecoveryError('候选进程不是 QQ 客户端，未加载')
            return pid, process.create_time(), executable
        except psutil.Error:
            raise RecoveryError('QQ 进程已变化，等待重新核对') from None

    async def ensure(self, cfg, *, allowed):
        if not cfg.snowluma_auto_connect or cfg.qq_provider != 'snowluma' or not allowed():
            return
        folder, port = self._installation(cfg)
        if not self._listener_matches(folder,port):
            if allowed():
                self._start(folder,port)
            raise RecoveryError('等待 SnowLuma 启动完成')
        if self.http:
            return await self._attach(self.http,cfg,folder,port,allowed)
        async with httpx.AsyncClient(trust_env=False,timeout=3) as http:
            await self._attach(http,cfg,folder,port,allowed)

    async def _attach(self, http, cfg, folder, port, allowed):
        if not allowed():
            return
        base = f'http://127.0.0.1:{port}'
        auth = await self._request(http,base,'/api/login',
                                   data={'password':cfg.snowluma_password.get_secret_value()})
        if (auth.get('success') is not True or auth.get('mustChangePassword')
                or auth.get('needsTotp') or auth.get('consentRequired')
                or not isinstance(auth.get('token'),str) or not auth['token']):
            raise RecoveryError('请先在 SnowLuma 控制台完成登录、安全验证或首次设置',needs_attention=True)
        token = auth['token']
        if not allowed():
            return
        snapshot = await self._request(http,base,'/api/processes',token=token)
        try:
            selected = self._select(snapshot,cfg.self_id)
        except RecoveryError:
            if await self._retry_owned(http,base,token,snapshot,cfg,folder,port,allowed):
                return
            raise
        if not allowed() or selected.get('injected') is True:
            return
        identity = self._qq_identity(selected['pid'])
        # Recheck account immediately before the PID-only vendor API; never fall back to another PID.
        verified = self._select(await self._request(http,base,'/api/processes',token=token),cfg.self_id)
        if (verified['pid'] != selected['pid'] or self._qq_identity(verified['pid']) != identity
                or not self._listener_matches(folder,port)):
            raise RecoveryError('QQ 或 SnowLuma 进程发生变化，等待重新核对')
        if not allowed() or verified.get('injected') is True:
            return
        result = await self._request(http,base,f"/api/processes/{verified['pid']}/load",token=token,data={})
        if result.get('success') is not True:
            raise RecoveryError('SnowLuma 未确认加载成功，稍后重新核对')
        owned = (folder,cfg.self_id,identity)
        if self._owned != owned:
            self._retried = False
        self._owned, self._loaded_at = owned, self.clock()
