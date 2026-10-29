"use strict";

const $ = (selector) => document.querySelector(selector);
const pages = {
  overview: ["运行概览", "查看本体状态、心跳与待处理任务。"],
  config: ["人格与设置", "管理角色、模型连接和日常作息。"],
  memories: ["记忆", "查看和修订记忆，保留人物及来源。"],
  "memory-settings": ["记忆设置", "设置各层记忆的读取、遗忘与增强策略。"],
  stickers: ["表情包", "从群聊中收藏含义明确的表情，按类别保留最新的一批。"],
  plugins: ["技能插件", "管理搜索、视频等可调用的能力。"],
  "activity-qq": ["QQ 聊天", "管理 QQ 账号、连接和授权会话。"],
  retrieval: ["记忆检索", "选择记忆检索方式，查看索引状态。"],
  records: ["运行记录", "按需查看聊天、回复和处理原因。"],
};
const labels = {setup:"等待配置", paused:"人工暂停", active:"活跃中", sleeping:"低频查看", night_pause:"夜间暂停", not_started:"尚未启动",
  unchecked:"待检查", available:"可用", unavailable:"等待恢复", needs_attention:"需要检查", disabled:"关闭",
  loading:"正在加载", ready:"已就绪", error:"需要检查", pending:"待处理", running:"处理中", retry_wait:"等待重试",
  succeeded:"已完成", skipped_uncertain:"发送结果不明，已跳过", skipped_recalled:"原消息已撤回", skipped_expired:"任务已过期或取消",
  skipped_duplicate:"同一消息已有回复，已跳过",
  skipped_silent:"保持沉默",
  core:"核心记忆", feeling:"情绪感受", context:"动态上下文", long_term:"长期记忆", important:"重要长期", secondary:"次要长期", recent:"近期记忆", greeting:"问候", scan:"话题判断", reply:"回复",
  tool:"工具任务", "uncertain fact must remain a candidate":"依据不足，暂存为候选记忆",
  scene:"场景分析", memory:"记忆整理", consolidate:"闲时记忆整理", initiative:"主动私聊", delivery:"回复", image:"图片理解", forward:"转发读取", "forward-images":"转发图片分批读取", config:"配置", decision:"处理判断", sticker:"表情包", qq:"QQ 接入"};
Object.assign(labels,{learning:"闲时学习",queued:"待学习",reading:"读取材料",thinking:"整理笔记",held:"手动暂停",abandoned:"已放弃",completed:"已学完",needs_source:"缺少可读资料"});
let page = "overview", config = null, status = null, offset = 0, recordType = "events", dirty = false;
let selectedConversation = "";
let activityPlugins = {};
let memoryQuery = "", memoryTier = "", stickerCategory = "", jobState = "";
const textLabel = (value) => labels[value] || value || "—";
const date = (timestamp) => timestamp ? new Date(timestamp * 1000).toLocaleString("zh-CN", {hour12:false}) : "—";

function icon(name) {
  const paths = {
    home:["M3 10 12 3l9 7v11h-6v-7H9v7H3Z"], file:["M14 3H5v18h14V8Z","M14 3v5h5M8 12h8M8 16h6"],
    memory:["M4 6c0-4 16-4 16 0s-16 4-16 0Z","M4 6v6c0 4 16 4 16 0V6M4 12v6c0 4 16 4 16 0v-6"],
    smile:["M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z","M8 14c2 3 6 3 8 0M8 9h.01M16 9h.01"],
    tool:["m14 7 3 3 4-4a6 6 0 0 1-7 8L6 22l-4-4 8-8a6 6 0 0 1 8-7Z"],
    cube:["m12 2 9 5v10l-9 5-9-5V7Z","m3 7 9 5 9-5M12 12v10M7 4.5l9 5"],
    pulse:["M2 12h5l3-8 4 16 3-8h5"],image:["M3 3h18v18H3Z","m3 17 6-7 6 7 3-4 3 4M15 7h.01"],
    refresh:["M20 8a8 8 0 0 0-14-3L3 8M3 3v5h5M4 16a8 8 0 0 0 14 3l3-3M21 21v-5h-5"],
    connect:["M7 3v5M17 3v5M5 8h14v3a7 7 0 0 1-14 0ZM12 18v4"]
  };
  const svg=document.createElementNS("http://www.w3.org/2000/svg","svg");
  for(const [key,value]of Object.entries({viewBox:"0 0 24 24",width:"24",height:"24",fill:"none",stroke:"currentColor","stroke-width":"1.8","stroke-linecap":"round","stroke-linejoin":"round","aria-hidden":"true"}))svg.setAttribute(key,value);
  for(const d of paths[name] || paths.file){const path=document.createElementNS(svg.namespaceURI,"path");path.setAttribute("d",d);svg.append(path);}
  return svg;
}

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "class") node.className = value;
    else if (key in node && !key.startsWith("aria")) node[key] = value;
    else node.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child !== null && child !== undefined && child !== false) node.append(child instanceof Node ? child : String(child));
  }
  return node;
}
function notify(message, error = false) {
  const node = $("#notice"); node.textContent = message; node.hidden = false;
  node.setAttribute("role", error ? "alert" : "status");
}
async function api(path, method = "GET", body) {
  if (selectedConversation && ["memories","stickers","sticker-categories","members","messages"].includes(path.split(/[?\/]/)[0])) {
    path += (path.includes("?") ? "&" : "?") + "conversation_id=" + encodeURIComponent(selectedConversation);
  }
  const response = await fetch("/api/" + path, {method, credentials:"same-origin",
    headers: method === "GET" ? {} : {"Content-Type":"application/json"},
    body: body === undefined ? undefined : JSON.stringify(body)});
  let result;
  try { result = await response.json(); } catch { throw new Error("本机服务返回异常，请查看运行日志。"); }
  if (!response.ok) {
    const error = new Error(typeof result.detail === "string" ? result.detail :
      Array.isArray(result.detail) ? "输入内容不符合要求，请检查填写格式和数值范围。" :
      result.detail?.message || `操作失败（${response.status}），请刷新后重试。`);
    error.detail = result.detail; error.status = response.status; throw error;
  }
  return result;
}
async function run(action, button) {
  if (button) button.disabled = true;
  try { return await action(); }
  catch (error) { notify(error.message || "本机服务连接失败，请确认服务已启动。", true); }
  finally { if (button) button.disabled = false; }
}
const button = (title, action, className = "") => h("button", {type:"button", class:className,
  onClick: event => run(action, event.currentTarget)}, title);
const panel = (...children) => h("section", {class:"panel"}, ...children);
const empty = (message) => h("div", {class:"empty"}, h("span", {class:"symbol", "aria-hidden":"true"}, icon("file")), message);
function table(headings, rows, emptyText = "暂无记录") {
  return h("div", {class:"table-wrap"}, h("table", {},
    h("thead", {}, h("tr", {}, headings.map(title => h("th", {scope:"col"}, title)))),
    h("tbody", {}, rows.length ? rows.map(cells => h("tr", {}, cells.map(cell => h("td", {}, cell)))) :
      h("tr", {}, h("td", {colSpan:headings.length}, empty(emptyText))))));
}
function pager(total, reload) {
  return h("div", {class:"pager"}, h("span", {}, `共 ${total} 条`),
    h("button", {type:"button", disabled:offset === 0, onClick:()=>run(async()=>{offset=Math.max(0,offset-50);await reload();})}, "上一页"),
    h("span", {}, `第 ${Math.floor(offset / 50) + 1} 页`),
    h("button", {type:"button", disabled:offset+50>=total, onClick:()=>run(async()=>{offset+=50;await reload();})}, "下一页"));
}
function switchControl(name, checked, onChange, enabledText = "已开启", disabledText = "关闭") {
  const stateText = h("span", {}, checked ? enabledText : disabledText);
  const input = h("input", {type:"checkbox", checked, "aria-label":name, onChange: async(event) => {
    input.disabled = true;
    try { await onChange(input.checked); stateText.textContent = input.checked ? enabledText : disabledText; }
    catch (error) { input.checked = !input.checked; notify(error.message, true); }
    finally { input.disabled = false; }
  }});
  return h("label", {class:"switch"}, input, h("span", {class:"switch-track", "aria-hidden":"true"}), stateText);
}
async function saveConfig(changes, revision = config.revision, confirmEvictions = false, evictionPreview = null, path = "config") {
  try { config = await api(path, "PATCH", {revision, changes, confirm_evictions:confirmEvictions, eviction_preview:evictionPreview}); }
  catch (error) {
    if (error.detail?.eviction_count && !confirmEvictions && confirm(`此修改将删除 ${error.detail.eviction_count} 张旧表情包，确定保存？`))
      return saveConfig(changes, revision, true, error.detail.eviction_preview, path);
    throw error;
  }
  return config;
}

function channelNotice() {
  if (status.preview_mode) return document.createDocumentFragment();
  if (!status.qq_connected && config.settings.qq_enabled) return h("div",{class:"connection-notice"},h("strong",{},"QQ 未连接"),"请检查 QQ 接入程序的登录状态和连接设置。");
  return document.createDocumentFragment();
}
async function loadPlugins() {
  const [data,skills]=await Promise.all([api("plugins"),api("skill-plugins")]);
  const installed=skills.items, ids=new Set(installed.map(item=>item.id));
  const toolNames=new Set(installed.flatMap(item=>item.tools.map(tool=>tool.name)));
  if(toolNames.has("web_search")&&toolNames.has("web_read"))ids.add("search");
  if(toolNames.has("video_read")&&toolNames.has("video_discover"))ids.add("video");
  const file=h("input",{type:"file",name:"package",accept:".zip,application/zip",required:true});
  const upload=h("form",{class:"filter-bar",onSubmit:event=>{
    event.preventDefault(); run(async()=>{
      const selected=file.files[0];
      if(!selected)throw new Error("请先选择插件包。");
      if(selected.size>8*1024*1024)throw new Error("插件包不能超过 8 MiB。");
      const response=await fetch("/api/skill-plugins/install",{method:"POST",credentials:"same-origin",
        headers:{"Content-Type":"application/zip"},body:selected});
      const result=await response.json();
      if(!response.ok)throw new Error(typeof result.detail==="string"?result.detail:"插件包安装失败，请核对文件。");
      await loadPlugins();notify("插件已安装，默认停用。确认设置后可启用。");
    },event.submitter);
  }},h("label",{class:"field"},"选择插件包",file),h("button",{type:"submit"},"安装"));
  const cards=installed.map(item=>panel(
    h("div",{class:"panel-heading"},h("h2",{},item.name),h("span",{class:"badge"},item.error?"载入异常":item.enabled?"已启用":"已停用")),
    h("p",{class:"hint"},`版本 ${item.version}`),h("p",{},item.description),
    h("p",{class:"hint"},"提供能力："+item.tools.map(tool=>({web_search:"搜索资料",web_read:"读取网页正文",video_discover:"发现公开视频",video_read:"读取视频片段",video_summarize:"总结视频内容"}[tool.name]||tool.description)).join("；")),
    item.error?h("p",{class:"hint"},item.error):null,
    h("div",{class:"item-actions"},button(item.enabled?"停用":"启用",async()=>{
      await api(`skill-plugins/${encodeURIComponent(item.id)}/enabled`,"PATCH",{revision:item.revision,enabled:!item.enabled});
      await loadPlugins();notify(item.enabled?"技能已停用，正在进行的调用可完成。":"技能已启用。");
    }),item.settings.length?button("设置",()=>skillSettingsEditor(item)):null,
    button("移除",()=>showEditor("移除技能",[
      h("p",{},`确定移除“${item.name}”？代码和设置将删除，技能自身的运行数据会保留，便于重新安装后恢复。`),
      h("p",{class:"hint"},"助手的人格、记忆和聊天记录由本体保存，不属于技能插件。")],
      ()=>api(`skill-plugins/${encodeURIComponent(item.id)}?revision=${item.revision}`,"DELETE",{}),"确认移除"),"danger"))));
  $("#content").replaceChildren(h("div",{class:"section-heading"},h("h2",{},"已安装技能")),
    panel(upload,h("p",{class:"hint"},"使用可信来源的 ZIP 插件包，最大 8 MiB。安装不会执行插件，启用后才允许助手调用。")),
    ...(cards.length?cards:[empty("暂无已安装的技能插件。")]),
    h("div",{class:"section-heading"},h("h2",{},"后续接入")),
    panel(table(["能力","用途","状态"],data.available_tools.filter(item=>!ids.has(item.id)).map(item=>[item.name,
      {search:"搜索资料并读取网页正文",video:"读取和总结视频内容",drawing:"生成或修改图片"}[item.id],"未接入"])),
      h("p",{class:"hint"},"技能插件为当前角色增加可调用的能力。人格和记忆由本体统一管理。")));
}
function skillSettingsEditor(item) {
  const configured=new Set(item.secret_fields_set);
  const fields=item.settings.map(setting=>h("div",{class:"field"},h("label",{},setting.label,
    h("input",{name:setting.name,type:setting.secret?"password":setting.type==="boolean"?"checkbox":setting.type==="integer"?"number":"text",
      ...(setting.type==="boolean"?{checked:item.config[setting.name]??setting.default}:
        {value:setting.secret?"":item.config[setting.name]??setting.default}),
      ...(setting.type==="integer"?{step:1,required:true}:{}),autocomplete:setting.secret?"new-password":"off"})),
    setting.secret?h("span",{class:"hint"},configured.has(setting.name)?"已设置，留空保持现有值":"尚未设置"):null,
    setting.secret&&configured.has(setting.name)?h("label",{},h("input",{type:"checkbox",name:"__clear_"+setting.name}),"清除此凭证"):null));
  showEditor(item.name+"设置",fields,form=>{
    const changes={};
    for(const setting of item.settings) {
      const value=form.get(setting.name);
      if(setting.secret) {
        if(form.has("__clear_"+setting.name))changes[setting.name]="";
        else if(value)changes[setting.name]=String(value);
      } else changes[setting.name]=setting.type==="boolean"?form.has(setting.name):setting.type==="integer"?Number(value):String(value??"");
    }
    return api(`skill-plugins/${encodeURIComponent(item.id)}/settings`,"PATCH",{revision:item.revision,changes});
  });
}
function saveActivityConfig(plugin, changes, revision=config.revision) {
  return saveConfig(changes,revision,false,null,`plugins/${encodeURIComponent(plugin.id)}/settings`);
}
function activitySettingsForm(plugin) {
  if(!plugin.settings_sections?.length)return empty("此活动范围没有可在此处修改的设置。");
  const form=configForm((changes,revision)=>saveActivityConfig(plugin,changes,revision));
  for(const section of plugin.settings_sections) form.append(configSection(section.title,section.description,
    section.fields.map(item=>field(item.key,item.label,item.type || "text",item.hint || "",item.options || null))));
  form.append(h("div",{class:"form-actions"},h("button",{type:"submit",class:"primary"},"保存设置")));
  return form;
}
async function loadActivity() {
  const plugin=activityPlugins[page];
  if(!plugin) {$("#content").replaceChildren(empty("此活动范围尚未安装或已移除。"));return;}
  if(plugin.id==="qq")return loadChannels(plugin);
  $("#content").replaceChildren(activitySettingsForm(plugin));
}
async function loadActivityNavigation() {
  const data=await api("plugins");
  const items=data.items.filter(item=>item.kind==="activity" && item.installed);
  activityPlugins=Object.fromEntries(items.map(item=>["activity-"+item.page_id,item]));
  $("#activity-nav").replaceChildren(h("p",{class:"nav-heading"},"活动范围"),...items.map(item=>{
    const key="activity-"+item.page_id;
    pages[key]=[item.name,item.description || `管理「${item.name}」的连接和授权。`];
    return h("a",{href:"#"+key,"data-page":key},h("span",{"aria-hidden":"true"},icon("connect")),item.name);
  }));
}
async function loadChannels(plugin) {
  const cfg=config.settings;
  const form=activitySettingsForm(plugin);
  const save=changes=>saveActivityConfig(plugin,changes);
  const groups=[...new Set([cfg.group_id,...cfg.group_ids].filter(Boolean))];
  const rows=groups.map(id=>["QQ 群聊",h("div",{},h("strong",{},id),h("small",{class:"row-note"},"群号")),h("span",{class:"badge"},"已授权"),
    button("移除",async()=>{
      if(dirty)throw new Error("请先保存 QQ 设置，再修改会话授权。");
      if(!confirm(`移除群 ${id} 的授权？已保存的记忆仍会保留。`))return;
      const remaining=groups.filter(x=>x!==id);
      await save({group_id:remaining[0]||"",group_ids:remaining.slice(1)});await refresh();notify("已移除群聊授权。");
    },"danger")]);
  if(cfg.owner_id) rows.push(["QQ 私聊",h("div",{},h("strong",{},cfg.owner_id),h("small",{class:"row-note"},"主号")),h("span",{class:cfg.owner_private_enabled?"badge":"muted"},cfg.owner_private_enabled?"已授权":"未授权"),
    button(cfg.owner_private_enabled?"关闭":"允许",async()=>{if(dirty)throw new Error("请先保存 QQ 设置，再修改会话授权。");await save({owner_private_enabled:!cfg.owner_private_enabled});await refresh();})]);
  for(const id of cfg.private_ids.filter(x=>x!==cfg.owner_id)) rows.push(["QQ 私聊",id,h("span",{class:"badge"},"已授权"),button("移除",async()=>{
    if(dirty)throw new Error("请先保存 QQ 设置，再修改会话授权。");
    if(!confirm(`移除与 ${id} 私聊的授权？`))return;
    await save({private_ids:cfg.private_ids.filter(x=>x!==id)});await refresh();notify("已移除私聊授权。");
  },"danger")]);
  const target=h("input",{name:"target",inputMode:"numeric",pattern:"[0-9]{1,20}",maxLength:20,required:true,placeholder:"输入群号",autocomplete:"off"});
  const targetLabel=h("span",{},"QQ 群号");
  const type=h("select",{name:"kind",onChange:()=>{targetLabel.textContent=type.value==="group"?"QQ 群号":"QQ 号";target.placeholder=type.value==="group"?"输入群号":"输入 QQ 号";}},h("option",{value:"group"},"群聊"),h("option",{value:"private"},"私聊"));
  const add=h("form",{class:"channel-add",onSubmit:event=>{
    event.preventDefault();run(async()=>{
      if(dirty)throw new Error("请先保存 QQ 设置，再修改会话授权。");
      const id=target.value.trim();
      if(!/^[0-9]{1,20}$/.test(id))throw new Error("请填写有效的数字号码。");
      let changes;
      if(type.value==="group") {
        if(groups.includes(id))throw new Error("该群已在授权列表中。");
        changes=cfg.group_id?{group_ids:[...cfg.group_ids,id]}:{group_id:id};
      } else {
        if(id===cfg.self_id)throw new Error("不能将机器人自己添加为私聊对象。");
        if(id===cfg.owner_id)changes={owner_private_enabled:true};
        else {if(cfg.private_ids.includes(id))throw new Error("该对象已在授权列表中。");changes={private_ids:[...cfg.private_ids,id]};}
      }
      await save(changes);await refresh();notify("会话授权已添加。");
    },event.submitter);
  }},h("label",{class:"field"},"类型",type),h("label",{class:"field"},targetLabel,target),h("button",{type:"submit",class:"primary"},"添加会话"));
  $("#content").replaceChildren(channelNotice(),h("section",{class:"channel-summary"},h("div",{class:"channel-identity"},icon("connect"),h("strong",{},"QQ 聊天"),h("span",{},cfg.qq_enabled?"已启用":"已停用"),h("span",{},status.qq_mode==="night_pause"?"夜间暂停":status.qq_connected?"已连接":"未连接")),button("连接设置",()=>{form.scrollIntoView({behavior:"smooth",block:"start"});})),
    !cfg.qq_enabled?h("p",{class:"connection-notice"},"QQ 聊天已停用。以下授权继续保留，重新启用后生效。",h("span",{},"可在本页的 QQ 账号与连接中重新启用。")):document.createDocumentFragment(),
    h("div",{class:"section-heading"},h("div",{},h("h2",{},"授权会话"),h("p",{class:"hint"},"加入群聊不会自动授权。仅处理下列会话中的消息。"))),
    table(["类型","会话","状态","操作"],rows,"尚未授权任何会话"),add,
    form);
}

async function loadOverview() {
  const cfg = config.settings;
  const jobs = await api("jobs?limit=6&pending_only=true");
  const nodes = [];
  if (!status.configured) nodes.push(h("section", {class:"setup-banner"}, h("span", {class:"symbol", "aria-hidden":"true"}, "ⓘ"),
    h("div", {}, h("h3", {}, "尚未完成配置"), h("p", {}, "请先设置当前角色和模型连接，再为活动范围添加会话授权。")), h("a", {href:"#config"}, "前往配置 →")));
  const statusItem = (name, title, value) => h("div", {class:"status-item"}, h("span", {class:"status-icon", "aria-hidden":"true"}, icon(name)),
    h("div", {}, h("small", {}, title), h("strong", {}, value)));
  nodes.push(h("section", {class:"status-strip", "aria-label":"本体运行状态"},
    statusItem("file", "当前角色", cfg.persona_name),
    statusItem("cube", "模型服务", textLabel(status.model_state || "unchecked")),
    statusItem("pulse", "当前状态", textLabel(status.mode))));
  const heartbeat = panel(h("div", {class:"panel-heading"}, h("h2", {}, "本体心跳"),h("span",{class:"badge"},textLabel(status.mode))),
    h("p",{},`活跃时每 ${cfg.active_poll_seconds} 秒查看新消息，空闲时每 ${cfg.sleeping_poll_seconds} 秒查看。`),
    h("p",{class:"hint"},"非作息暂停期间，收到名字呼叫可立即唤醒。作息暂停时停止心跳和后台 AI，管理页仍可访问。"),
    h("p",{class:"hint"},status.last_heartbeat?"最近心跳："+date(status.last_heartbeat):"等待心跳状态。"),
    button("心跳设置",()=>{location.hash="config";}));
  const retrieval = panel(h("div", {class:"panel-heading"}, h("h2", {}, "记忆检索"),
    h("span", {class:"badge"}, cfg.embedding_provider === "none" ? "关闭" : textLabel(status.embedding?.state))),
    h("p", {}, cfg.embedding_provider === "none" ? "当前使用关键词检索，再由 DS 筛选结果。也可以开启语义检索。" :
      `当前使用${cfg.embedding_provider === "local" ? "本地 CPU 模型" : "外部 embedding 接口"}。${status.embedding?.error || "可随时关闭，关闭后释放本地模型内存。"}`),
    button("检索设置", ()=>{location.hash="retrieval";}, "primary wide-button"));
  const pending = jobs.items.filter(item=>["pending","running","retry_wait","needs_attention"].includes(item.state));
  const tasks = panel(h("div", {class:"panel-heading"}, h("h2", {}, "待处理任务"), h("a", {href:"#records"}, `查看记录 · ${status.pending_jobs}`)),
    table(["类型", "状态", "时间"], pending.map(job=>[textLabel(job.kind),textLabel(job.state),date(job.created_at)]), "暂无待处理任务"));
  const activity = panel(h("h2", {}, "活动范围"),h("p",{},"每个活动范围插件独立管理账号、连接与收发规则。"),
    h("p",{class:"hint"},"在侧栏选择已安装的活动范围查看设置。"));
  nodes.push(h("div", {class:"overview-grid"}, heartbeat, retrieval, tasks, activity));
  $("#content").replaceChildren(...nodes);
}

function field(name, title, type = "text", hint = "", options = null) {
  let input;
  if (options) input = h("select", {name}, options.map(([value,label])=>h("option", {value, selected:String(config.settings[name])===value}, label)));
  else if (type === "textarea") input = h("textarea", {name, value:Array.isArray(config.settings[name]) ? config.settings[name].join("\n") : config.settings[name] || "", rows:name === "persona" ? 7 : 3});
  else if (type === "checkbox") return h("div", {class:"field"}, h("label", {class:"check-field"}, h("input", {name, type, checked:config.settings[name], "aria-label":title}), title), hint && h("small",{},hint));
  else input = h("input", {name, type, value:type==="password" ? "" : name === "aliases" ? config.settings.aliases.join("，") : config.settings[name] ?? "",
    autocomplete:type === "password" ? "new-password" : "off", placeholder:type==="password" ?
      (config.settings[name+"_set"] ? "已保存，留空保留" : "尚未设置") : ""});
  if (type === "number") input.min = "1";
  if (name === "group_id" || name === "self_id") { input.inputMode = "numeric"; input.pattern = "[0-9]*"; }
  return h("label", {class:"field" + (type==="textarea" ? " full" : "")}, title, input, hint && h("small", {}, hint));
}
function configSection(title, description, fields) {
  return h("section", {class:"panel form-section"}, h("h2", {}, title), h("p", {class:"hint"}, description), h("div", {class:"form-grid"}, fields));
}
function configForm(saver=saveConfig) {
  const initial = structuredClone(config);
  const form = h("form", {onInput:()=>{dirty=true;}, onSubmit:event=>{
    event.preventDefault(); run(async()=>{
      const changes = {};
      for (const input of form.elements) {
        if (!input.name) continue;
        let value = input.type === "checkbox" ? input.checked : input.type === "number" ?
          (input.value === "" && input.name === "drawing_daily_limit" ? null : Number(input.value)) : input.value;
        if (input.type === "password" && value === "") continue;
        if (["aliases","group_ids","private_ids","learning_topics","learning_sources"].includes(input.name)) value = value.split(/[,，\n]/).map(x=>x.trim()).filter(Boolean);
        if (JSON.stringify(value) !== JSON.stringify(initial.settings[input.name])) changes[input.name] = value;
      }
      if (!Object.keys(changes).length) { notify("没有需要保存的修改。"); dirty=false; return; }
      await saver(changes, initial.revision); dirty=false; await refresh(); notify(status.preview_mode ? "预览配置已保存。" : "配置已保存并将自动应用。密钥不会回显。");
    }, event.submitter);
  }});
  return form;
}
async function loadConfig(retrievalOnly = false) {
  const form=configForm();
  if (!retrievalOnly) {
    form.append(configSection("当前人格", "所有授权会话共用当前角色。切换角色后，未发送的回复会重新生成，原角色档案继续保留。", [
      field("persona_name","角色名称"),field("persona","固定核心人设","textarea","只保留稳定身份、基础性格、关系和边界。表达习惯与场景参照在核心记忆中管理。"), field("aliases","机器人称呼","text","多个称呼以中文或英文逗号分隔。"),
      field("runtime_enabled","允许运行","checkbox","关闭后暂停收件、回复与后台 AI；管理页仍可使用。"),


      h("p",{class:"hint"},h("a",{href:"#activity-qq"},"前往 QQ 聊天设置账号、连接和授权会话"))]),
    configSection("模型连接", "通过你配置的兼容 API 网关调用模型。密钥留空会保留已保存值；首次配置按服务端要求填写，免鉴权网关可使用自定义非敏感占位值。", [
      field("workbuddy_url","模型 API 地址","url"),field("workbuddy_key","API 密钥","password"),
      field("primary_model","主意识模型","text","固定使用 DS，填写网关提供的准确模型名称。"),
      field("auxiliary_model","后台辅助模型"),
      field("scene_analysis_enabled","辅助对话核对","checkbox","后台梳理对象与话题，并核对普通回复中的无依据状态、行动承诺和串题。"),field("reviewer_model","独立复核模型"),
      h("p",{class:"hint"},`主意识：${config.settings.primary_model}。DS 故障时保留任务等待恢复，其他模型不代答。`)]),
    configSection("本体作息", "为断电前预留休息时间。暂停时不处理聊天或后台 AI，仅保留管理页和定时恢复；不自动发送早晚安。", [
      field("routine_enabled","启用作息模式","checkbox"),field("bedtime","开始休息时间","time"),
      field("reboot_wakeup","开机后最早恢复时间","time"),field("continuous_wakeup","持续供电恢复时间","time")]),
    configSection("本体心跳与记忆", "本体在工作时段按活跃与空闲节奏处理任务。作息暂停期间停止心跳、收件、发言和后台 AI。", [
      field("active_poll_seconds","活跃时查看间隔（秒）","number"),field("inactive_after_seconds","转为空闲状态的等待时间（秒）","number"),
      field("sleeping_poll_seconds","空闲时查看间隔（秒）","number"),field("background_concurrency","后台并发任务数","number"),
      field("retention_days","原始聊天保留（天）","number"),
      h("p",{class:"hint"},"已保存的长期记忆持续保留，可在记忆页面修订。")]) );
  } else {
    form.append(configSection("语义检索", "数据库检索与大模型筛选始终可用。语义检索可选；关闭后释放本地模型内存。", [
      field("embedding_provider","语义检索方式","text","本地模式使用 CPU，不占用显卡。",[["none","关闭"],["local","本地小模型（CPU）"],["api","外部 embedding API"]]),
      field("local_embedding_model","本地模型"),field("embedding_download_allowed","允许首次下载本地模型","checkbox"),
      field("local_threads","本地 CPU 线程数","number"),field("local_batch_size","本地批量大小","number"),
      field("embedding_url","外部 embedding API 地址","url"),field("embedding_model","外部 embedding 模型"),
      field("embedding_key","外部 embedding 密钥","password"),field("embedding_dimensions","外部向量维度","number")]),
      h("p", {class:"hint"}, `当前状态：${textLabel(status.embedding?.state || "disabled")}。${status.embedding?.error || "本地模型需要先安装可选依赖，首次下载需要联网。"}`));
  }
  form.append(h("div", {class:"form-actions"}, h("button", {type:"submit",class:"primary"}, "保存配置")));
  const nodes = [form];
  if (!retrievalOnly) {
    const profiles = await api("personas");
    const select = h("select", {"aria-label":"选择已有的人格"}, profiles.items.map(p=>h("option", {value:p.id,selected:p.id===profiles.active_id},p.name)));
    nodes.push(panel(h("h2",{},"人格档案"),h("p",{class:"hint"},"切换对全部会话生效。编辑当前人格请使用上方表单。"),select,
      button("切换人格",async()=>{
        if(dirty && !window.confirm("有未保存的配置，切换将放弃这些修改。是否继续？"))return;
        const profile=profiles.items.find(p=>p.id===select.value);
        if(!window.confirm(`将所有会话的当前角色切换为「${profile.name}」？`))return;
        await api(`personas/${encodeURIComponent(profile.id)}/switch`,"POST",{revision:config.revision,persona_revision:profile.revision});
        dirty=false;await refresh();notify("当前人格已切换。");
      }),button("新建人格",()=>showEditor("新建人格",[
        editorField("id","档案标识（小写字母、数字、下划线或连字符）",""),editorField("name","角色名称",""),
        editorField("persona","核心人设","","textarea"),editorField("aliases","唤醒称呼（逗号分隔）","")
      ],async data=>{await api("personas","POST",{id:data.get("id"),name:data.get("name"),persona:data.get("persona"),
        aliases:data.get("aliases").split(/[,，\n]/).map(x=>x.trim()).filter(Boolean)});}))));
  }
  if (retrievalOnly) {

    nodes.push(panel(h("h2", {}, "重新同步记忆索引"),h("p", {}, "根据已保存的记忆更新检索索引，不修改记忆内容。"),
      button("重新同步索引", async()=>{await api("embedding/reindex","POST",{});notify("已安排重新同步索引。");})));
  }
  $("#content").replaceChildren(...nodes);
}

async function learningPanel() {
  const data=await api("learning");
  const add=h("form",{class:"filter-bar",onSubmit:event=>{
    event.preventDefault();run(async()=>{
      await api("learning","POST",{url:add.elements.url.value.trim(),part:Number(add.elements.part.value)});
      await refresh();notify("资料已加入学习队列；关闭学习或运行暂停时不会读取。");
    },event.submitter);
  }},h("label",{class:"field"},"视频地址",h("input",{name:"url",type:"url",required:true,placeholder:"https://www.bilibili.com/video/BV…/"})),
    h("label",{class:"field"},"分 P",h("input",{name:"part",type:"number",value:1,min:1,max:10000,required:true})),
    h("button",{type:"submit"},"加入队列"));
  const control=(item,action,title)=>button(title,async()=>{
    if(action==="abandon" && !confirm("放弃这条学习任务？已保存的知识和笔记仍会保留。"))return;
    await api(`learning/${item.id}/${action}`,"POST",{});await refresh();
  });
  return panel(h("h2",{},"学习队列与检查点"),
    h("p",{class:"hint"},"已读表示材料已保存，已学表示笔记已提交。字幕或少量抽帧不代表已完整理解声音与画面。"),add,
    table(["资料","状态","进度","操作"],data.items.map(item=>[
      h("a",{href:item.url+(item.part>1?`?p=${item.part}`:""),target:"_blank",rel:"noreferrer"},`${item.title || item.url} · P${item.part}`),
      h("div",{},h("div",{style:"white-space:nowrap"},textLabel(item.state)),item.reason && h("p",{class:"hint"},item.reason)),
      `已读 ${Math.floor(item.read_seconds)} 秒 / 已学 ${Math.floor(item.cursor_seconds)} 秒`,
      ["completed","abandoned"].includes(item.state)?"—":h("div",{class:"item-actions"},
        control(item,"pause","暂停"),control(item,"resume","继续"),control(item,"abandon","放弃"))
    ]),"暂无学习资料"),
    h("h3",{},"聊天中的视频候选"),
    h("p",{class:"hint"},"这里只记录推荐出处，由当前角色在闲时选择；列入候选不表示已经观看。"),
    table(["视频","推荐来源","收到时间"],(data.recommendations||[]).map(item=>[
      h("a",{href:item.url+(item.part>1?`?p=${item.part}`:""),target:"_blank",rel:"noreferrer"},`${item.url} · P${item.part}`),
      `${item.group_id.startsWith("private:")?"私聊":"群"} ${item.group_id.replace("private:","")} · QQ ${item.user_id} · 消息 ${item.source_id}`,
      date(item.created_at)
    ]),"暂无聊天推荐"),
    h("h3",{},"最近学习笔记"),...data.notes.map(note=>h("article",{class:"memory-item"},
      h("a",{href:note.url+(note.part>1?`?p=${note.part}`:""),target:"_blank",rel:"noreferrer"},`${note.title} · P${note.part} · ${Math.floor(note.start_seconds)}–${Math.floor(note.end_seconds)} 秒`),
      h("p",{},note.summary),h("small",{},`UP 主：${note.uploader} · ${{sampled_frames:"抽样画面，未分析声音",timeline_frames:"分段时序画面，音轨范围见笔记"}[note.coverage]||"仅字幕"} · ${date(note.created_at)}`),
      note.suggestions.length?h("p",{class:"hint"},`待审建议（未自动执行）：${note.suggestions.join("；")}`):null)));
}

function showEditor(title, fields, save, submitLabel="保存") {
  $("#edit-title").textContent = title; $("#edit-error").textContent = "";
  $("#edit-form").querySelector('[type="submit"]').textContent=submitLabel;
  $("#edit-fields").replaceChildren(...fields);
  $("#edit-form").onsubmit = async(event)=>{
    event.preventDefault(); event.submitter.disabled = true;
    try { await save(new FormData(event.currentTarget)); $("#editor").close(); await refresh(); notify("修改已保存。"); }
    catch(error) { $("#edit-error").textContent = error.message; }
    finally { event.submitter.disabled=false; }
  };
  $("#editor").showModal();
}
const editorField = (name,title,value,type="text") => h("label", {class:"field"}, title,
  h(type==="textarea" ? "textarea" : "input",{name,value,required:name!=="tags",...(type==="textarea"?{rows:5}:{type})}));
async function loadMemories() {
  const v2=config.memory_schema_version===2;
  const factualLayers=v2?["recent","long_term"]:["recent","secondary","important"];
  const layerOptions=["core","context","feeling",...factualLayers];
  const fractionField=(name,label,value,min=0,max=1)=>h("label",{class:"field"},label,
    h("input",{name,type:"number",min,max,step:"any",value,required:true}));
  const query = new URLSearchParams({query:memoryQuery, offset, limit:50}); if (memoryTier) query.set("tier",memoryTier);
  const settingsOnly=page==="memory-settings";
  const result = settingsOnly?{items:[],total:0}:await api("memories?"+query);
  const mood=v2 && !settingsOnly?await api("mood"):null;
  const emotionLabels=mood?.emotion_labels || {};
  const profileFields=(values={},arousal=null)=>[
    h("details",{},h("summary",{},"具体情绪分量（0至1，可同时存在）"),
      h("div",{class:"filter-bar"},Object.entries(emotionLabels).map(([key,label])=>fractionField(`emotion_${key}`,label,values[key]??0)))),
    h("label",{class:"field"},"平静／激动程度（0至1，留空未记录）",h("input",{name:"arousal",type:"number",min:0,max:1,step:"any",value:arousal??""})),
    h("p",{class:"hint"},"0 表示平静，1 表示很激动，与感受有多强分开；各类情绪分量不超过整体感受强度，不要求加起来等于 1。")];
  const readProfile=form=>({emotions:Object.fromEntries(Object.keys(emotionLabels).map(key=>[key,Number(form.get(`emotion_${key}`))]).filter(([,value])=>value>0)),
    arousal:form.get("arousal")===""?null:Number(form.get("arousal"))});
  const describeEmotions=values=>Object.entries(values||{}).filter(([,value])=>value>=.005)
    .map(([key,value])=>`${emotionLabels[key]||key} ${Math.round(value*100)}%`).join(" · ")||"未记录具体类别";
  const search = h("form", {class:"filter-bar", onSubmit:event=>{event.preventDefault();memoryQuery=search.elements.query.value;memoryTier=search.elements.tier.value;offset=0;run(loadMemories);}},
    h("label",{class:"field"},"检索内容",h("input",{name:"query",value:memoryQuery,placeholder:"关键词或群友记忆"})),
    h("label",{class:"field"},"记忆层级",h("select",{name:"tier"}, [["","会话记忆"],...layerOptions.map(value=>[value,textLabel(value)])].map(([value,label])=>h("option",{value,selected:value===memoryTier},label)))),
    h("button",{type:"submit"},"筛选"));
  const policies = config.settings.memory_policies || {};
  const policyForm = h("form",{onInput:()=>{dirty=true;},onSubmit:event=>{event.preventDefault();run(async()=>{
    const next=structuredClone(policies);
    for(const layer of Object.keys(next)){
      if(!["core","context"].includes(layer)){
        next[layer].strategy=policyForm.elements[`strategy_${layer}`].value;
        const value=policyForm.elements[`half_${layer}`].value;
        next[layer].half_life_minutes=value===""?null:Number(value);
      }
      if(layer==="context")next[layer].window_batches=Number(policyForm.elements.window_context.value);
      if(factualLayers.includes(layer)){
        if(v2){
          next[layer].curve_shape=policyForm.elements[`curve_${layer}`].value;
        }
        next[layer].forget_threshold=Number(policyForm.elements[`forget_${layer}`].value)/100;
        next[layer].reinforcement_gain=Number(policyForm.elements[`reinforce_${layer}`].value)/100;
        next[layer].merge_recall_gain=Number(policyForm.elements[`merge_${layer}`].value)/100;
        if(v2)next[layer].spacing_half_life_minutes=Number(policyForm.elements[`spacing_${layer}`].value);
        if(layer==="recent")for(const key of ["distilled_secondary_days","distilled_important_days"])
          next[layer][key]=Number(policyForm.elements[key].value);
      }
      if(layer==="feeling"){
        for(const key of ["low_days","high_days","recall_gain"])next[layer][key]=Number(policyForm.elements[key].value);
        const threshold=policyForm.elements.permanent_threshold.value;
        next[layer].permanent_threshold=threshold===""?null:Number(threshold)/100;
        if(v2)for(const key of ["perceptibility","forget_threshold"])next[layer][key]=Number(policyForm.elements[`feeling_${key}`].value);
      }
      if(v2 && !["core","context"].includes(layer)){
        next[layer].dynamics={...next[layer].dynamics};
        for(const input of policyForm.querySelectorAll(`[data-dynamics-layer="${layer}"]`))next[layer].dynamics[input.dataset.key]=Number(input.value);
      }
    }
    await saveConfig({memory_policies:next,
      memory_deleted_retention_days:Number(policyForm.elements.deleted_retention.value),
      memory_relation_expansion:policyForm.elements.memory_relation_expansion.checked,
      memory_relation_depth:Number(policyForm.elements.memory_relation_depth.value),
      ...(v2?{mood_half_life_minutes:Number(policyForm.elements.mood_half_life.value)}:{})});dirty=false;await loadMemories();
    notify(status.preview_mode?"预览记忆策略已保存。":"记忆策略已保存。");
  },event.submitter);}},
    ...Object.entries(policies).map(([layer,value])=>h("div",{class:"filter-bar"},h("strong",{},textLabel(layer)),
      ["core","context"].includes(layer)?h("span",{class:"hint"},layer==="core"?"始终全量读取，不衰减":"固定滚动，全量读取，不衰减"):
        h("label",{class:"field"},"读取倾向",h("select",{name:`strategy_${layer}`},[["relevance","相关性优先"],["recency","时效优先"],["importance","重要性优先"]].map(([key,label])=>h("option",{value:key,selected:key===value.strategy},label)))),
      ...(["core","context"].includes(layer)?[]:[h("label",{class:"field"},layer==="feeling"?"感受基准半衰期（分钟，留空不平复）":v2?"遗忘基准半衰期（分钟，留空使用极长保留）":"半衰期（分钟，留空不衰减）",h("input",{name:`half_${layer}`,type:"number",min:1,max:5256000,step:"any",value:value.half_life_minutes ?? ""}))]),
      ...(layer==="context"?[h("label",{class:"field"},"保留批数",h("input",{name:"window_context",type:"number",min:1,max:50,value:value.window_batches}))]:[]))),
    ...factualLayers.filter(layer=>policies[layer]).map(layer=>h("fieldset",{class:"panel"},
      h("legend",{},`${textLabel(layer)} · 遗忘与增强`),h("div",{class:"filter-bar"},
      ...[["forget","遗忘阈值（%，0 关闭）","forget_threshold",99.99],
          ["reinforce","新强调增强比例（%）","reinforcement_gain",100],
          ["merge","重复合并增强比例（%）","merge_recall_gain",100]].map(([name,label,key,max])=>
        h("label",{class:"field"},label,h("input",{name:`${name}_${layer}`,type:"number",min:0,max,step:"any",value:policies[layer][key]*100}))),
      ...(v2?[h("label",{class:"field"},"遗忘曲线",h("select",{name:`curve_${layer}`},[["exponential","单指数（原曲线）"],["dual","双阶段（快降与长尾）"]].map(([key,label])=>h("option",{value:key,selected:key===(policies[layer].curve_shape||"exponential")},label)))),
        h("label",{class:"field"},"确认间隔尺度（分钟）",h("input",{name:`spacing_${layer}`,type:"number",min:1,max:10080,step:"any",value:policies[layer].spacing_half_life_minutes})),
        h("p",{class:"hint"},"双阶段曲线将密集确认计作一轮，间隔回想主要增强巩固并部分回退有效年龄，不恢复缺失细节。原曲线保留旧增强方式。同一来源重读不重复计入。")]:[]),
      ...(layer==="recent"?[["distilled_secondary_days",v2?"提炼后原近期半衰期下界（天）":"提炼为次要后，原近期半衰期（天）"],
        ["distilled_important_days",v2?"提炼后原近期半衰期上界（天）":"提炼为重要后，原近期半衰期（天）"]].map(([name,label])=>
          h("label",{class:"field"},label,h("input",{name,type:"number",min:1,max:36500,step:"any",value:policies.recent[name]}))):[])),
      h("p",{class:"hint"},"基准半衰期用于校准初始遗忘速度，不是删除期限；双阶段后期衰减更慢。普通读取不增强，人工维护条目受保护。切换曲线保留当前程度。"))),
    ...(policies.feeling?[h("div",{class:"filter-bar"},
      ...[["low_days",v2?"情绪记忆遗忘半衰期下界（天）":"低强度遗忘时长（天）",.01,36500],["high_days",v2?"情绪记忆遗忘半衰期上界（天）":"高强度遗忘时长（天）",.01,36500],["recall_gain","回想增强比例（0至1）",0,1]].map(([key,label,min,max])=>h("label",{class:"field"},label,h("input",{name:key,type:"number",min,max,step:"any",value:policies.feeling[key]}))),
      h("label",{class:"field"},"不遗忘的强度门槛（%，留空关闭）",h("input",{name:"permanent_threshold",type:"number",min:1,max:100,step:"any",value:policies.feeling.permanent_threshold==null?"":policies.feeling.permanent_threshold*100})))]:[]),
    ...(v2?[h("div",{class:"filter-bar"},
      fractionField("feeling_perceptibility","感受归零门槛（0至1）",policies.feeling.perceptibility,.000001,.999999),
      fractionField("feeling_forget_threshold","感受记忆删除门槛（0至1）",policies.feeling.forget_threshold,.000001,.999999),
      fractionField("mood_half_life","当前心情半衰期（分钟）",config.settings.mood_half_life_minutes,1,10080)),
      ...Object.entries(policies).filter(([layer])=>!["core","context"].includes(layer)).map(([layer,value])=>
        h("details",{class:"panel"},h("summary",{},`${textLabel(layer)} · 曲线与检索参数`),
          h("p",{class:"hint"},"意义、细节和检索参数独立于事实置信度。修改后保留当前程度，再采用新曲线。"),
          h("div",{class:"filter-bar"},Object.entries({...(layer==="feeling"?{}:{minimum_half_life:"有效半衰期下界（秒）",maximum_half_life:"有效半衰期上界（秒）",emotion_half_life:"情绪残留基准半衰期（秒）"}),
            emotion_minimum_half_life:"情绪平复半衰期下界（秒）",emotion_maximum_half_life:"情绪平复半衰期上界（秒）",
            emotion_meaning_influence:"意义对情绪平复的影响",meaning_influence:"意义对半衰期的影响",consolidation_influence:"巩固对半衰期的影响",
            emotional_influence:"情绪对半衰期的影响",interference_influence:"干扰对半衰期的影响",detail_ratio:"细节保留比例",
            emotion_detail_protection:"情绪对细节的保护",consolidation_detail_protection:"巩固对细节的保护",interference_detail_influence:"干扰对细节的影响",
            significance_weight:"检索意义权重",emotion_weight:"检索情绪权重",cue_weight:"检索线索权重",recency_weight:"检索时效权重",
            recall_weight:"检索巩固权重",interference_weight:"检索干扰权重",recall_bias:"检索门槛偏移",recall_threshold:"检索触发门槛"}).map(([key,label])=>
              h("label",{class:"field"},label,h("input",{type:"number",step:"any",required:true,
                "data-dynamics-layer":layer,"data-key":key,value:value.dynamics?.[key]??config.memory_dynamics_defaults[key]}))))))]:[]),
    h("label",{class:"field"},"删除后正文保留天数",h("input",{name:"deleted_retention",type:"number",min:1,max:365,required:true,value:config.settings.memory_deleted_retention_days??7})),
    h("p",{class:"hint"},"到期后清除已删除条目的正文、正文历史和相关派生图记录，保留防止旧任务恢复内容的最小标记。备份与原始聊天另有保留周期。"),
    h("fieldset",{class:"panel"},h("legend",{},"关系与图谱检索"),
      h("label",{class:"check-field"},h("input",{name:"memory_relation_expansion",type:"checkbox",checked:config.settings.memory_relation_expansion}),"启用关系与图谱召回"),
      h("label",{class:"field"},"最大关联跳数",h("select",{name:"memory_relation_depth"},[1,2,3].map(value=>h("option",{value,selected:value===config.settings.memory_relation_depth},`${value} 跳`)))),
      h("p",{class:"hint"},"沿已有来源的关系寻找补充记忆，不把关联推断成事实。关闭后仍可使用数据库检索和可选语义检索。")),
    h("p",{class:"hint"},"感受先平复，归零后才开始遗忘；普通记忆按各层曲线衰减，达到已启用的遗忘阈值后清理。固定人设不自动改写，核心记忆可随真实经历成长，动态上下文按批次滚动。"),h("button",{type:"submit"},"保存分层策略"));
  if(settingsOnly){
    $("#content").replaceChildren(h("p",{},h("a",{href:"#memories"},"← 返回记忆")),
      panel(h("h2",{},"分层记忆策略"),policyForm));return;
  }
  const items = result.items.map(item=>item.tier==="context"?h("article",{class:"panel memory-item"},
    h("div",{class:"item-meta"},h("span",{class:"badge"},"动态上下文"),h("span",{},date(item.created_at)),h("span",{},textLabel(item.state))),
    item.summary?h("div",{},h("h3",{},"辅助整理"),...item.summary.items.map(part=>h("p",{class:"item-text"},part.summary))):null,
    h("h3",{},"本批消息"),item.messages.map(message=>h("p",{class:"item-text"},
      `${message.display_name || message.nickname || message.user_id}：${message.recalled?"[已撤回]":message.text || "[图片或其他消息]"}`)),
    item.reason?h("p",{class:"hint"},item.reason):null,
    h("h3",{},"回复情况"),item.replies.length?item.replies.map(reply=>h("div",{},
      h("p",{class:"hint"},reply.send_status==="sent"?"已发送":textLabel(reply.state)),
      reply.segments.map(segment=>h("p",{class:"item-text"},segment.type==="text"?segment.data.text:"[图片或其他内容]")))):
      h("p",{class:"hint"},item.already_answered.length?"重复补问，已有回复":item.state==="succeeded"?"本批保持沉默":"等待判断")):
    h("article",{class:"panel memory-item"},
    h("div",{class:"item-meta"},h("span",{class:"badge"},textLabel(item.tier)),h("span",{},item.tier==="core"?"当前角色共用":item.tier==="feeling"?"当前角色的感受":item.subject_id ? `QQ ${item.subject_id}` : "会话记忆"),h("span",{},`版本 ${item.revision}`),h("span",{},date(item.updated_at))),
    h("p",{class:"item-text"},item.content),h("div",{class:"hint"},item.tags.join(" · ")),
    item.tier==="feeling" && item.decay_factor!=null?h("p",{class:"hint"},`当前感受 ${Math.round(item.feeling_level*100)}%，初始强度 ${Math.round(item.initial_feeling*100)}%，记忆剩余 ${Math.round(item.memory_remaining*100)}%`):null,
    factualLayers.includes(item.tier)&&item.memory_remaining!=null?h("p",{class:"hint"},
      `记忆剩余 ${(item.memory_remaining*100).toFixed(1)}% · 半衰期 ${item.memory_half_life_minutes==null?"不衰减":`${(item.memory_half_life_minutes/1440).toFixed(2)} 天`} · ${item.memory_protected?"人工维护保护":"按分层策略维护"}`):null,
    item.dynamics_version===2?h("details",{},h("summary",{},"记忆指标"),h("p",{class:"hint"},
      `意义 ${(item.meaning_strength*100).toFixed(1)}% · 细节 ${(item.detail_integrity*100).toFixed(1)}% · 情绪残留 ${(item.emotional_residue*100).toFixed(1)}% · 巩固 ${(item.consolidation*100).toFixed(1)}% · 干扰 ${(item.interference*100).toFixed(1)}%`),
      item.memory_retention_status?h("p",{class:"hint"},item.memory_retention_status==="active"?`预计达到遗忘阈值：${date(item.memory_threshold_at)}`:
        ({protected:"人工维护保护，不自动遗忘",disabled:"未启用自动遗忘阈值",no_decay:"当前不衰减",reached:"已达到遗忘阈值，等待维护处理",out_of_range:"预测超出可显示日期范围",unavailable:"当前不参与读取"})[item.memory_retention_status]):null,
      item.memory_retention_status?h("p",{class:"hint"},`最近记录的增强：${item.memory_last_reinforced_at==null?"暂无记录":date(item.memory_last_reinforced_at)}`):null,
      item.memory_retention_status?h("p",{class:"hint"},"预测按当前参数且无新增强计算，达到阈值不等于已经删除。文字编辑和普通查看不算增强。"):null,
      item.tier==="feeling"?h("p",{class:"hint"},`情绪方向 ${(item.feeling_valence??0).toFixed(2)}（-1 负向，0 中性或未确定，1 正向）`):null,
      item.tier==="feeling"?h("p",{class:"hint"},`具体感受：${describeEmotions(item.feeling_emotions)}；平静／激动程度：${item.feeling_arousal==null?"未记录":`${Math.round(item.feeling_arousal*100)}%`}`):null,
      h("p",{class:"hint"},"检索可及性随当前问题的线索计算，不等于事实置信度。")):null,
    ...[["distillation_sources","提炼来源"],["distillation_targets","已提炼的要点"]].map(([key,label])=>
      item[key]?.length?h("details",{},h("summary",{},`${label}（${item[key].length}）`),
        ...item[key].map(link=>h("div",{},h("p",{class:"hint"},`原版本 ${link.revision} · ${date(link.created_at)} · ${link.current?"版本一致":"记录已变化，保留原提炼关系"}`),
          h("p",{class:"item-text"},link.content || "原版本内容不在此展示。")))):null),
    item.relations?.length?h("details",{},h("summary",{},`记忆关联（${item.relations.length}）`),
      ...item.relations.map(link=>h("div",{},
        h("p",{class:"hint"},({duplicate:"内容重复",correction:"前后纠正",related:"相关经历"})[link.kind] || "记忆关联"),
        h("p",{class:"item-text"},link.summary),
        h("p",{class:"item-text"},`关联原文：${link.source_id===item.id?link.target_content:link.source_content}`),
        h("p",{class:"hint"},`关联时版本：${link.source_revision} / ${link.target_revision}；原记忆修改后自动失效`)))):null,
    h("div",{class:"item-actions"},button("编辑",()=>showEditor("编辑记忆",[
      editorField("content","记忆内容",item.content,"textarea"), editorField("tags","标签（逗号分隔）",item.tags.join("，")),
      h("label",{class:"field"},"层级",h("select",{name:"tier"},(item.tier==="core"?["core"]:[...(item.persona_id && !item.subject_id?["feeling"]:[]),...factualLayers]).map(value=>h("option",{value,selected:value===item.tier},textLabel(value))))),
      ...(item.dynamics_version===2?[fractionField("significance","初始意义强度（0至1）",item.initial_meaning_strength)]:[]),
      ...(v2 && item.tier==="feeling"?[fractionField("strength","初始感受强度（0至1）",item.initial_feeling),
        fractionField("valence","情绪方向（-1至1）",item.feeling_valence,-1,1),...profileFields(item.feeling_emotions,item.feeling_arousal)]:[])],
      form=>{
        const changes={revision:item.revision,content:form.get("content"),tier:form.get("tier"),tags:String(form.get("tags")).split(/[,，]/).map(x=>x.trim()).filter(Boolean)};
        for(const [field,key,old] of [["significance","significance",item.initial_meaning_strength],["strength","feeling_strength",item.initial_feeling],["valence","feeling_valence",item.feeling_valence]])
          if(form.has(field) && Number(form.get(field))!==old)changes[key]=Number(form.get(field));
        if(v2 && item.tier==="feeling" && changes.tier==="feeling"){
          const profile=readProfile(form);
          if(Object.keys(emotionLabels).some(key=>(profile.emotions[key]||0)!==(item.feeling_emotions?.[key]||0)))changes.feeling_emotions=profile.emotions;
          if(profile.arousal!==item.feeling_arousal)changes.feeling_arousal=profile.arousal;
        }
        return api("memories/"+item.id,"PATCH",changes);
      })),
      button("删除",async()=>{if(confirm("删除这条记忆？已删除内容不会被旧的后台结果恢复。")){await api(`memories/${item.id}?revision=${item.revision}`,"DELETE");await loadMemories();}},"danger"))));
  $("#content").replaceChildren(
    h("div",{class:"memory-toolbar"},search,
      button("记忆设置",()=>{location.hash="#memory-settings";}),
      button(memoryTier==="core"?"查看会话记忆":"核心记忆",()=>{memoryTier=memoryTier==="core"?"":"core";offset=0;run(loadMemories);})),
    ...(mood?[h("details",{class:"panel"},h("summary",{},`当前心情 · 方向 ${mood.valence.toFixed(2)} · 强度 ${Math.round(mood.intensity*100)}%`),
      h("p",{class:"hint"},`正向 ${Math.round(mood.positive*100)}% · 负向 ${Math.round(mood.negative*100)}% · 半衰期 ${mood.half_life_minutes} 分钟 · 最近变化 ${date(mood.updated_at)}`),
      h("p",{class:"item-text"},`具体感受：${describeEmotions(mood.emotions)}`),
      h("p",{class:"hint"},`平静／激动程度：${mood.arousal==null?"未记录":`${Math.round(mood.arousal*100)}%`}`),
      h("p",{class:"hint"},"当前角色共用的即时状态，随真实互动变化并逐渐平复，与长期人格和历史情绪记忆分开。"),
      h("details",{},h("summary",{},"最近变化依据"),...(mood.changes?.length?mood.changes.map(change=>
        h("div",{},h("p",{class:"item-text"},`${change.kind==="explicit_recall"?"主动回想：":""}${change.reason}`),
          h("p",{class:"hint"},`${date(change.time)}${change.memory_id?` · 来源记忆 ${change.memory_id.slice(0,8)} · 版本 ${change.memory_revision}`:""}`))):[h("p",{class:"hint"},"暂无有效的近期变化依据") ])),
      button("调整当前心情",()=>showEditor("调整当前心情",[fractionField("valence","方向（-1至1）",mood.valence,-1,1),fractionField("intensity","强度（0至1）",mood.intensity),...profileFields(mood.emotions,mood.arousal)],
        form=>api("mood","POST",{revision:config.revision,operation_id:crypto.randomUUID(),valence:Number(form.get("valence")),intensity:Number(form.get("intensity")),...readProfile(form)}))),
      button("恢复平静",async()=>{await api("mood","POST",{revision:config.revision,operation_id:crypto.randomUUID(),reset:true});await loadMemories();}))]:[]),
    ...(memoryTier==="core"?[panel(h("div",{class:"panel-heading"},h("h2",{},"核心记忆"),
      button("新增核心记忆",()=>showEditor("新增核心记忆",[editorField("content","记忆内容","","textarea")],
        form=>api("memories","POST",{config_revision:config.revision,content:form.get("content"),tier:"core"})))),
      h("p",{class:"hint"},"当前角色共用，可根据真实经历整理和成长。表达习惯与场景参照在这里维护。"),
      h("a",{href:"#config"},"修改固定核心人设 →"))]:[]),
    ...(memoryTier==="feeling"?[panel(h("p",{class:"hint"},"保存当前角色在真实互动中的感受、缘由和变化，按会话保留隐私边界，不改变核心设定。"),
      button("新增情绪感受",()=>showEditor("新增情绪感受",[editorField("content","感受与相关经历","","textarea"),h("label",{class:"field"},"初始感受强度（%）",h("input",{name:"strength",type:"number",min:0,max:100,value:50})),
        ...(v2?[fractionField("valence","情绪方向（-1至1）",0,-1,1),fractionField("significance","意义强度（0至1）",.5),...profileFields()]:[])],
        form=>api("memories","POST",{config_revision:config.revision,content:form.get("content"),tier:"feeling",feeling_strength:Number(form.get("strength"))/100,
          ...(v2?{feeling_valence:Number(form.get("valence")),significance:Number(form.get("significance")),
            feeling_emotions:readProfile(form).emotions,feeling_arousal:readProfile(form).arousal}:{})}))))]:[]),
    ...(memoryTier==="context"?[panel(h("p",{class:"hint"},`按当前会话保留最近 ${policies.context?.window_batches || 5} 批消息和实际回复，随新批次滚动更新。此层全量读取，用于衔接对话；长期记忆另行保存。`))]:[]),
    h("div",{class:"memory-list"},items.length?items:empty("尚无符合条件的记忆")),pager(result.total,loadMemories));
}
async function loadStickers() {
  const params = new URLSearchParams({offset,limit:50}); if(stickerCategory)params.set("category",stickerCategory);
  const [result,categories] = await Promise.all([api("stickers?"+params),api("sticker-categories")]);
  const select = h("select",{"aria-label":"表情包类别",onChange:()=>{stickerCategory=select.value;offset=0;run(loadStickers);}},
    h("option",{value:""},"全部类别"),categories.items.map(item=>h("option",{value:item.name,selected:item.name===stickerCategory},`${item.name}（${item.count}）`)));
  const items = result.items.map(item=>h("article",{class:"panel sticker"},h("img",{src:"/api/media/"+item.id,alt:item.meaning,loading:"lazy"}),
    h("h3",{},item.category),h("p",{},item.meaning),h("p",{},date(item.collected_at)),
    h("div",{class:"item-actions"},button("编辑",()=>showEditor("编辑表情包",[
      editorField("category","类别",item.category),editorField("meaning","明确含义",item.meaning,"textarea"),editorField("tags","标签（逗号分隔）",item.tags.join("，"))],
      form=>api("stickers/"+item.id,"PATCH",{revision:item.revision,category:form.get("category"),meaning:form.get("meaning"),tags:String(form.get("tags")).split(/[,，]/).map(x=>x.trim()).filter(Boolean)}))),
      button("删除",async()=>{if(confirm("确定移除这张表情包？")){await api(`stickers/${item.id}?revision=${item.revision}`,"DELETE");await loadStickers();}},"danger"))));
  $("#content").replaceChildren(h("div",{class:"filter-bar"},h("label",{class:"field"},"类别",select),
    h("p",{class:"hint"},`每类最多 ${config.settings.sticker_per_category} 张，总量最多 ${config.settings.sticker_total} 张；按收藏时间替换旧表情。`)),
    items.length?h("div",{class:"sticker-grid"},items):panel(empty("表情包库为空，群聊中遇到明确、可复用的表情后会自动收藏。")),pager(result.total,loadStickers));
}
async function loadRecords() {
  const params = new URLSearchParams({offset,limit:50}); if(recordType==="jobs" && jobState)params.set("state",jobState);
  const result = await api(recordType+"?"+params);
  const tabs = h("div",{class:"record-tabs"},[["events","决定与操作"],["messages","聊天记录"],["jobs","后台任务"],["members","成员资料"]].map(([key,title])=>
    button(title,async()=>{recordType=key;offset=0;await loadRecords();},recordType===key?"active":"")));
  let headings, rows;
  if (recordType==="events") {
    headings=["时间","类型","简短说明","详情"];
    rows=result.items.map(item=>[date(item.time),textLabel(item.kind),item.summary || item.reason || item.message || "—",
      h("details",{},h("summary",{},"展开"),h("pre",{},JSON.stringify(item.details,null,2)))]);
  } else if(recordType==="messages") {
    headings=["时间","群友","内容"];
    rows=result.items.map(item=>[date(item.time),`${item.message.display_name || item.user_id} · ${item.user_id}`,
      h("div",{},h("p",{class:"item-text"},item.message.text || "[图片或其他消息]"), item.recalled ? h("span",{class:"badge"},"已撤回") : null)]);
  } else if(recordType==="members") {
    headings=["QQ 号","当前群昵称","昵称历史"];
    rows=result.items.map(item=>[item.user_id,item.display_name || item.nickname || "—",h("pre",{},JSON.stringify(item.aliases || [],null,2))]);
  } else {
    headings=["时间 / 类型","状态","最近情况","操作"];
    rows=result.items.map(item=>[h("div",{},textLabel(item.kind),h("p",{class:"hint"},date(item.created_at))),item.kind==="delivery"&&item.state==="held"?"等待发送":textLabel(item.state),
      h("div",{},textLabel(item.last_error),h("p",{class:"hint"},`已尝试 ${item.attempts} 次`)),
      ["needs_attention","retry_wait"].includes(item.state)?button("重试",async()=>{await api(`jobs/${item.id}/retry`,"POST",{});await loadRecords();}):"—"]);
  }
  const filter = recordType==="jobs" ? h("div",{class:"filter-bar"},h("label",{class:"field"},"任务状态",
    h("select",{onChange:event=>{jobState=event.target.value;offset=0;run(loadRecords);}},["","pending","running","retry_wait","needs_attention","succeeded","skipped_uncertain"].map(value=>
      h("option",{value,selected:value===jobState},value?textLabel(value):"全部状态"))))) : h("p",{class:"hint"},recordType==="messages" ?
      `原始记录保留约 ${config.settings.retention_days} 天。只显示已授权会话的记录。` : "记录回复、保持沉默和收藏表情包的简短原因，按需刷新查看。");
  $("#content").replaceChildren(tabs,filter,table(headings,rows),pager(result.total ?? result.items.length,loadRecords));
}
async function refresh() {
  $("#content").setAttribute("aria-busy","true");
  try {
    [config,status] = await Promise.all([api("config"),api("status")]);
    const cfg=config.settings;
    const environment=$("#environment"); environment.hidden=!status.preview_mode;
    environment.textContent=status.preview_mode?"独立预览：使用测试配置，不连接 QQ，也不会发送消息。":"";
    const groups=[...new Set([cfg.group_id,...cfg.group_ids].filter(Boolean))];
    const choices=groups.map(id=>["group:"+id,"群 "+id]);
    if(cfg.owner_private_enabled && cfg.owner_id) choices.push(["private:"+cfg.owner_id,"主号私聊 "+cfg.owner_id]);
    for(const id of cfg.private_ids || []) if(!choices.some(([key])=>key==="private:"+id)) choices.push(["private:"+id,"私聊 "+id]);
    if(page==="memories") choices.push(["knowledge:public","公开学习知识"]);
    if(!choices.some(([id])=>id===selectedConversation)) selectedConversation=choices[0]?.[0] || "";
    const controls=$("#scope-controls");
    controls.hidden=!["memories","stickers","records"].includes(page);
    controls.replaceChildren(h("label",{class:"field"},"当前查看会话",h("select",{"aria-label":"当前查看会话",onChange:event=>{
      selectedConversation=event.target.value;offset=0;run(refresh);
    }},choices.map(([value,label])=>h("option",{value,selected:value===selectedConversation},label)))));
    if(page==="overview") await loadOverview(); else if(page==="config")await loadConfig();
    else if(page==="retrieval")await loadConfig(true); else if(["memories","memory-settings"].includes(page))await loadMemories();
    else if(page==="stickers")await loadStickers();else if(page==="plugins")await loadPlugins();else if(page.startsWith("activity-"))await loadActivity();else await loadRecords();
    $("#updated").textContent = "最近读取："+new Date().toLocaleTimeString("zh-CN",{hour12:false})+" · 按需刷新";
  } finally { $("#content").setAttribute("aria-busy","false"); }
}
async function navigate() {
  await loadActivityNavigation();
  if(location.hash==="#channels")history.replaceState(null,"","#activity-qq");
  const next = location.hash.slice(1) in pages ? location.hash.slice(1) : "overview";
  if(dirty && next!==page && !confirm("配置尚未保存，确定离开？")){history.replaceState(null,"","#"+page);return;}
  dirty=false;page=next;offset=0;$("#notice").hidden=true;
  $("#page-title").textContent=pages[page][0];$("#page-subtitle").textContent=pages[page][1];
  document.title=pages[page][0]+" · BuwuBot";
  for(const link of document.querySelectorAll("nav a")){if(link.dataset.page===(page==="memory-settings"?"memories":page))link.setAttribute("aria-current","page");else link.removeAttribute("aria-current");}
  await refresh();
}
$("#refresh").addEventListener("click",event=>run(async()=>{
  if(dirty && !confirm("配置尚未保存，刷新会放弃修改。确定刷新？"))return;
  dirty=false;$("#notice").hidden=true;await refresh();
},event.currentTarget));
$("#close-editor").addEventListener("click",()=>$("#editor").close());
$("#cancel-editor").addEventListener("click",()=>$("#editor").close());
window.addEventListener("hashchange",()=>run(navigate));
window.addEventListener("beforeunload",event=>{if(dirty){event.preventDefault();event.returnValue="";}});
run(navigate);
for(const [key,name]of Object.entries({overview:"home",config:"file",memories:"memory",stickers:"smile",plugins:"tool",retrieval:"memory",records:"file"})) {
  document.querySelector(`a[data-page="${key}"] span`)?.replaceChildren(icon(name));
}
$("#refresh span").replaceChildren(icon("refresh"));
