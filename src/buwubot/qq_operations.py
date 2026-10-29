"""Narrow self-profile operations belonging to the QQ channel, not a second sender."""

import hashlib
import json
import re

from .coordinator import WorkPaused
from .qq import QQError

DESCRIPTION = ('操作自己的QQ资料：get_self读取账号和昵称；update_self目前仅支持fields.avatar，'
               '从image_analyses的asset_hash或已提供的表情资源id直接选图，无需让群友登记或计算编号。'
               '只能操作机器人自己，不接受网址、文件路径、任意QQ接口或其他账号。')
SCHEMA = {'type': 'object', 'required': ['operation'], 'additionalProperties': False,
          'properties': {'operation': {'type': 'string', 'enum': ['get_self', 'update_self']},
                         'fields': {'type': 'object', 'additionalProperties': False,
                                    'properties': {'avatar': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}}}}}


class QQOperations:
    def __init__(self, store, coordinator, media, adapter, settings, *, allowed):
        self.store, self.coordinator, self.media = store, coordinator, media
        self.adapter, self.settings, self.allowed = adapter, settings, allowed

    async def __call__(self, args):
        if set(args) - {'operation', 'fields', 'request_id', 'operation_id'}:
            raise ValueError('unknown QQ operation fields')
        operation = args.get('operation')
        fields = args.get('fields', {})
        if operation not in {'get_self', 'update_self'} or not isinstance(fields, dict):
            raise ValueError('invalid QQ operation')
        if operation == 'get_self' and fields:
            raise ValueError('get_self does not accept update fields')
        if operation == 'update_self' and (set(fields) != {'avatar'}
                or not isinstance(fields['avatar'], str)
                or not re.fullmatch('[a-f0-9]{64}', fields['avatar'])):
            raise ValueError('update_self requires a registered avatar hash')
        request = args.get('request_id', '')
        if operation == 'update_self' and (not isinstance(request, str) or not 1 <= len(request) <= 500):
            raise ValueError('QQ update requires a stable request ID')

        async def perform():
            cfg, qq = self.settings(), self.adapter()
            def check():
                current = self.settings()
                if (not self.allowed() or qq is None or not qq.connected or self.adapter() is not qq
                        or current.self_id != cfg.self_id
                        or current.persona_fingerprint != cfg.persona_fingerprint):
                    raise WorkPaused('QQ操作等待连接和允许运行的时段')
                if not cfg.self_id or cfg.self_id == cfg.owner_id:
                    raise QQError('未确认独立机器人账号，未执行QQ操作')
            check()
            key = 'qq_self_operation:' + hashlib.sha256(
                json.dumps([cfg.self_id, cfg.persona_id, request]).encode()).hexdigest()
            latest_key = 'qq_self_latest:' + cfg.self_id
            previous = self.store.get_state(key)
            fingerprint = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
            if operation == 'update_self':
                if args.get('operation_id') not in (None, '', key):
                    raise ValueError('QQ operation identity changed')
                if previous:
                    if previous['fingerprint'] != fingerprint:
                        raise ValueError('QQ update arguments changed during retry')
                    if previous.get('result'):
                        return previous['result']
                    if self.store.get_state(latest_key) != key:
                        return {'state': 'failed', 'message': '旧资料操作已被后续操作替代，未覆盖当前资料'}
                path = self.media.path(fields['avatar'])
                if not path.is_file():
                    raise FileNotFoundError('registered image unavailable')
            profile = await qq.call('get_login_info')
            check()
            if str(profile.get('user_id', '')) != cfg.self_id:
                raise QQError('QQ实际登录账号不匹配，未执行资料修改')
            if operation == 'get_self':
                return {'state': 'succeeded', 'message': '已读取自己的QQ资料',
                        'data': {'user_id': cfg.self_id, 'nickname': str(profile.get('nickname', ''))[:200]}}
            if not previous:
                with self.store.transaction():
                    self.store.set_state(key, {'fingerprint': fingerprint})
                    self.store.set_state(latest_key, key)
            # Retrying an uncertain assignment uses the identical image, never a new action/body.
            # A later operation supersedes an older unconfirmed one instead of reverting the profile.
            await qq.call('set_qq_avatar', {'file': path.as_uri()})
            result = {'state': 'succeeded', 'message': 'QQ已确认自己的资料更新',
                      'data': {'user_id': cfg.self_id, 'updated_fields': ['avatar']}}
            self.store.set_state(key, {'fingerprint': fingerprint, 'result': result})
            return result

        return await self.coordinator.effect(perform)
