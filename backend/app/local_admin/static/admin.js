const view = document.querySelector("#view");
const title = document.querySelector("#title");
const message = document.querySelector("#message");
const names = {tasks:"任务状态", tables:"数据表", docs:"接口文档", config:"运行配置"};
const docNames = {"platform-api.md":"公共接口目录","api-requests.md":"请求参数与校验","field-consumers.md":"字段来源与用途","news-api.md":"快讯","market-supply.md":"市场","macro-supply.md":"宏观","workbench-api.md":"工作台","screener-api-fields.md":"选股器","security-search.md":"证券搜索","portfolio-split-lineage.md":"组合"};
let token, generation = 0, refresh;
const el = (tag, text, cls) => {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (cls) node.className = cls;
  return node;
};
const action = (label, fn, cls) => {
  const b = el("button", label, cls);
  b.onclick = async () => {
    const version = generation;
    b.disabled = true;
    try { await fn(); } catch (e) { if (generation === version) message.textContent = e.message; }
    finally { b.disabled = false; }
  };
  return b;
};
function link(label, href) {
  const a = el("a", label);
  a.href = href;
  return a;
}
function date(value) {
  return value ? new Date(value).toLocaleString("zh-CN", {hour12:false}) : "—";
}
async function api(path, data) {
  const options = data ? {method:"POST", headers:{"Content-Type":"application/json","X-Admin-Token":token},body:JSON.stringify(data)} : {};
  const response = await fetch(path, options);
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw Error(typeof error.detail === "string" ? error.detail : "读取失败（" + response.status + "），请刷新后重试");
  }
  return response.json();
}
const query = (page, data={}) => "#" + page + "?" + new URLSearchParams(data);
function grid(headings, rows) {
  const frame = el("div", null, "frame"), table = el("table"), head = el("tr");
  headings.forEach(h => head.append(el("th",h)));
  table.createTHead().append(head);
  const body = table.createTBody();
  for (const values of rows) {
    const tr = body.insertRow();
    for (const value of values) {
      const cell = tr.insertCell();
      cell.append(value instanceof Node ? value : document.createTextNode(String(value ?? "—")));
    }
  }
  frame.append(table);
  return frame;
}
function pager(prev,next,go) {
  const box = el("div",null,"pager");
  for (const [label,value] of [["上一页",prev],["下一页",next]]) {
    const button = action(label,()=>go(value));
    button.disabled = value == null;
    box.append(button);
  }
  return box;
}
async function taskPage(box, p) {
  const tasks = await api("/admin/api/tasks");
  box.append(grid(["采集任务","调度规则","当前状态","配置应用","下次触发","运行记录"], tasks.map(t=>[
    link(t.title,query("tasks",{task:t.name})), t.rule, el("span",t.activity,"pill " + (t.active?"active":"")),
    t.applyState, date(t.next_run), t.result || "暂无回执"
  ])));
  if (!p.has("task")) return;
  const t = tasks.find(t=>t.name===p.get("task"));
  if (!t) return;
  const panel = el("section",null,"panel");
  box.append(panel);
  panel.append(el("h2",t.title),el("p",t.description),el("p","配置来源 · " + t.source),
    el("p","最近开始 " + date(t.started) + "　结束 " + date(t.finished)),
    el("p","工作进程 " + (t.workerAlive?"在线":"未运行") + "　回执时间 " + date(t.seen)));
  const offset = Number(p.get("offset") || 0);
  const base = "/admin/api/tasks/" + t.name + "/runs";
  const history = await api(base + "?offset=" + offset);
  panel.append(link("浏览相关表 " + history.related.join("."),query("tables",{s:history.related[0],t:history.related[1]})));
  if (history.heartbeat && t.name!=="news") panel.append(el("p","数据库最近一次调度心跳 " + date(history.heartbeat.last_heartbeat_at)));
  if (history.cursor) panel.append(el("p","最近游标 " + history.cursor.cursor_key + " · " + date(history.cursor.updated_at)));
  panel.append(el("h3","历史运行"));
  if (history.runs.length) {
    panel.append(grid(["开始时间","来源","处理行","请求数","耗时","结果","关联记录"],history.runs.map(r=>[
      date(r.started_at),r.trigger||"未登记",r.row_count,r.request_count,r.duration==null?"—":r.duration.toFixed(1)+" 秒",r.displayStatus,
      link("查看",query("tasks",{task:t.name,offset,run:r.id}))])));
    panel.append(pager(offset?Math.max(0,offset-20):null,history.more?offset+20:null,
      n=>location.hash=query("tasks",{task:t.name,offset:n})));
  } else panel.append(el("p","暂无同名作业历史，可查看上方进程回执与相关表。","muted"));
  if (p.has("run")) {
    const detail = await api(base + "/" + encodeURIComponent(p.get("run")));
    const links = el("div",null,"toolbar");
    for(const [s,table] of detail.relations) links.append(link(table,query("tables",{s,t:table,run:p.get("run")})));
    panel.append(el("h3","本轮关联记录"), links);
  }
}
async function tablePage(box,p) {
  const tables = await api("/admin/api/tables");
  if (!tables.length) { box.append(el("p","当前连接没有可浏览的应用表")); return; }
  const selected = tables.find(x=>x.schema===p.get("s")&&x.name===p.get("t")) || tables[0];
  const s=selected.schema,t=selected.name, toolbar=el("div",null,"toolbar"), choose=el("select");
  choose.setAttribute("aria-label","数据表");
  for(const [index,item] of tables.entries()) {
    const option = new Option(item.schema+"."+item.name,String(index),false,item===selected);
    choose.add(option);
  }
  choose.onchange=()=>{const item=tables[Number(choose.value)]; location.hash=query("tables",{s:item.schema,t:item.name});};
  toolbar.append(choose); box.append(toolbar);
  const base="/admin/api/tables/"+encodeURIComponent(s)+"/"+encodeURIComponent(t);
  const code=p.get("run"), params=new URLSearchParams({cursor:p.get("cursor")||0});
  if(code) params.set("run",code);
  const batch=p.get("batch");
  if(batch) params.set("batch",batch);
  const page=await api(base+"?"+params);
  if(code) box.append(el("p","运行编号 "+code,"mono"));
  if(batch) box.append(el("p","采集批次 "+batch,"mono"));
  const fields=el("details"); fields.open=true;
  fields.append(el("summary","字段说明（"+page.columns.length+"）"),
    grid(["字段","类型","说明"],page.columns.map(c=>[el("code",c.name),c.type,c.comment||"暂无说明"])));
  box.append(fields,el("h2","记录"),el("p",page.orderHint,"muted"));
  box.append(grid(page.columns.map(c=>c.name),page.rows.map(row=>row.values.map((v,i)=>{
    const part=el("div"), value=el("span",v===null?"NULL":v,"gridcell mono");
    part.append(value);
    if(s==="raw" && t==="ingestion_batch" && page.columns[i].name==="id"){
      const links=el("div",null,"toolbar");
      links.append(link("本批原件",query("tables",{s,t:"source_payload",batch:v})),
        link("拒收记录",query("tables",{s,t:"rejected_record",batch:v})));
      part.append(links);
    }
    if(row.more[i]) {
      let start=0, full="";
      const expand=action("展开",async()=>{
        const chunk=await api(base+"/cell?"+new URLSearchParams({column:page.columns[i].name,row:row.id,start}));
        full+=chunk.value; value.textContent=full; value.className="mono gridcell";
        start=chunk.next;
        if(start==null) expand.remove(); else expand.textContent="继续读取";
      });
      part.append(expand);
    }
    return part;
  }))));
  if (!page.rows.length) box.append(el("p","此页没有记录","notice"));
  box.append(pager(page.previousCursor,page.nextCursor,n=>location.hash=query("tables",{s,t,cursor:n,...(code?{run:code}:{}),...(batch?{batch}:{})})));
}
async function docsPage(box) {
  const links=el("div",null,"toolbar");
  links.append(link("Swagger UI","/docs"),link("OpenAPI JSON","/openapi.json"));
  box.append(links,el("h2","公共数据接口"));
  const spec=await api("/openapi.json");
  for(const [path,methods] of Object.entries(spec.paths)) {
    if(!path.startsWith("/api/v1/")) continue;
    for(const method of Object.keys(methods)) {
      const row=el("div",null,"docrow");
      row.append(el("strong",method.toUpperCase()),el("code",path));
      box.append(row);
    }
  }
  box.append(el("h2","接口与字段说明"));
  const index=el("div",null,"toolbar");
  for(const [file,label] of Object.entries(docNames)) index.append(link(label,"/admin/docs/"+file));
  box.append(index);
}
async function configPage(box) {
  const tasks=await api("/admin/api/tasks");
  for(const [id,label] of [["tasks","定时采集开关"],["market","市场共用调度时刻"],["news","新闻轮询间隔（秒）"]]) {
    const saved=await api("/admin/api/config/"+id), group=el("fieldset");
    group.append(el("legend",label));
    const inputs={};
    for(const [key,value] of Object.entries(saved.fields)) {
      const wrapper=el("label"),input=el("input");
      input.type=id==="tasks"?"checkbox":id==="market"?"time":"number";
      if(id==="tasks") input.checked=value; else input.value=value;
      if(id==="news"){input.min=1;input.max=86400;}
      inputs[key]=input;
      wrapper.append(input,document.createTextNode(tasks.find(t=>t.name===key)?.title||label));
      group.append(wrapper);
    }
    const feedback=el("p",null,"muted");
    const apply=action("应用",async()=>{
      const changes={};
      for(const [key,input] of Object.entries(inputs)) {
        if(!input.reportValidity()) return;
        changes[key]=id==="tasks"?input.checked:id==="news"?Number(input.value):input.value;
      }
      const response=await api("/admin/api/config/"+id+"/apply",{version:saved.version,changes});
      saved.version=response.version;
      feedback.textContent="配置已保存。请在任务状态中查看运行方回执。";
    },"primary");
    if(id==="market") group.append(el("p","同时影响基础行情、市场脉搏、地域、ETF 热力图和商品行情。","muted"));
    group.append(el("br"),apply,feedback);box.append(group);
  }
  box.append(el("h2","文件配置的调度规则"), grid(["任务","生效规则","来源"],tasks.filter(t=>["stocks","announcements","macro"].includes(t.name)).map(t=>[t.title,t.rule,t.source])));
}
async function render(background=false) {
  clearTimeout(refresh);
  const current=++generation;
  const [key,search=""]=location.hash.slice(1).split("?");
  const section=Object.hasOwn(names,key)?key:"tasks";
  if(!background){message.textContent="";view.textContent="正在读取…";}
  title.textContent=names[section];
  document.querySelectorAll("nav a").forEach(a=>{if(a.hash==="#"+section)a.setAttribute("aria-current","page");else a.removeAttribute("aria-current");});
  const box=document.createDocumentFragment();
  try{
    if(!token)token=(await api("/admin/api/session")).token;
    await ({tasks:taskPage,tables:tablePage,docs:docsPage,config:configPage})[section](box,new URLSearchParams(search));
    if(current===generation){view.replaceChildren(box);message.textContent="";}
  }catch(e){if(current===generation){message.textContent=e.message;if(!background)view.replaceChildren(box);}}
  if(current===generation && section==="tasks")refresh=setTimeout(()=>render(true),8000);
}
window.addEventListener("hashchange",()=>render());
render();
