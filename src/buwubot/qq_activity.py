"""QQ plugin settings; routine and heartbeat belong to the core."""

QQ_SETTINGS_SECTIONS = [
    {"title": 'QQ 账号与连接',
     "description": '使用独立机器人账号，通过 OneBot 接入程序连接 QQ。访问令牌留空保留原值。',
     "fields": [
         {'key': 'qq_enabled', 'label': '启用 QQ 聊天', 'type': 'checkbox', 'hint': '停用后停止 QQ 收件和发言，保留授权、人格、记忆和未完成任务。'},
         {'key': 'qq_provider', 'label': 'QQ 接入程序', 'options': [['napcat', 'NapCat'], ['snowluma', 'SnowLuma']],
          'hint': '与实际运行的接入程序保持一致。切换前先验证新连接，保留原有账号数据。'},
         {'key': 'self_id', 'label': '机器人 QQ 号', 'hint': '填写接入程序中已登录的机器人账号，连接时核对身份。'},
         {'key': 'owner_id', 'label': '主号 QQ'},
         {'key': 'qq_url', 'label': 'OneBot HTTP 地址', 'type': 'url'},
         {'key': 'qq_ws_url', 'label': 'OneBot WebSocket 地址'},
         {'key': 'qq_token', 'label': 'OneBot HTTP 访问令牌', 'type': 'password'},
         {'key': 'qq_ws_token', 'label': 'OneBot WebSocket 访问令牌', 'type': 'password',
          'hint': '未设置时使用 HTTP 访问令牌。填写接入程序已有的 WebSocket 令牌；保存时留空保留原值。'},
     ]},
    {"title": '本机自动恢复',
     "description": '连接中断时尝试启动已安装的 SnowLuma，仅加载已确认的机器人账号。遵守本体作息，不自动登录 QQ。',
     "fields": [
         {'key':'snowluma_auto_connect','label':'自动恢复 SnowLuma 连接','type':'checkbox',
          'hint':'默认关闭。未知账号、重复账号或身份变化时等待，不启用全局自动加载。'},
         {'key':'snowluma_directory','label':'SnowLuma 安装目录',
          'hint':'填写已手动完成首次设置的完整目录，包含 node.exe 和 index.mjs。'},
         {'key':'snowluma_password','label':'SnowLuma 控制台密码','type':'password',
          'hint':'使用已有控制台密码，与 QQ 密码不同。保存在本机，留空保留原值；安全验证仍由你在原控制台完成。'},
     ]},
    {"title": '主动私聊主号',
     "description": '允许助手在空闲时自己决定是否找主号聊天，可以保持沉默，不设定时问候。',
     "fields": [
         {'key':'owner_initiative_enabled','label':'允许主动私聊主号','type':'checkbox',
          'hint':'默认关闭。仅使用已授权的主号私聊；未获回应时隔较长时间才再考虑，不连续催促。'},
         {'key':'owner_initiative_interval_minutes','label':'主动聊天判断间隔（分钟）','type':'number',
          'hint':'最少 15 分钟。只是空闲时考虑聊天的间隔，不保证每次发言；作息暂停时不判断。'},
         {'key':'owner_initiative_unanswered_hours','label':'未回复时再次考虑的间隔（小时）','type':'number',
          'hint':'默认 72 小时，至少 24 小时。之后仍可保持沉默；发送结果不明时先等待核对，不重复发起。'},
     ]},
    {"title": 'QQ 回复与表情包',
     "description": '按 QQ 会话授权收发消息，表情包按机器人账号共用。',
     "fields": [
         {'key': 'history_reply_hours', 'label': '离线消息通常可回复时限（小时）', 'type': 'number'},
         {'key': 'qq_ambient_reply_delay_seconds', 'label': '非定向话题观察时间（秒）', 'type': 'number',
          'hint': '默认15秒，可设0–300秒。群聊未明确叫到助手时先观察，之后仍可沉默；@、名字呼叫、回复助手及私聊不额外等待。'},
         {'key': 'sticker_per_category', 'label': '每类表情包上限', 'type': 'number'},
         {'key': 'sticker_total', 'label': '表情包总量上限', 'type': 'number'},
     ]},
]

QQ_CONFIGURATION_FIELDS = frozenset(
    field["key"] for section in QQ_SETTINGS_SECTIONS for field in section["fields"]
) | {"group_id", "group_ids", "private_ids", "owner_private_enabled"}
