const node=(tag,text,cls)=>{
  const e=document.createElement(tag);
  if(text!=null)e.textContent=text;
  if(cls)e.className=cls;
  return e;
};
function link(text,href){
  const e=node("a",text);e.href=href;return e;
}
function judgment(j){
  return (j?.value===true?"是。":j?.value===false?"否。":"待核验。")+" "+(j?.explanation||"缺少说明");
}
function table(head,rows){
  const wrap=node("div",null,"frame"),t=node("table"),tr=t.createTHead().insertRow();
  head.forEach(h=>tr.append(node("th",h)));
  for(const row of rows){
    const tr=t.createTBody().insertRow();
    for(const value of row)tr.insertCell().append(value instanceof Node?value:document.createTextNode(String(value??"—")));
  }
  wrap.append(t);return wrap;
}
function heading(box,text){
  box.append(node("h2",text));
}
function fold(title,content){
  const d=node("details");d.append(node("summary",title),content);return d;
}
function refLink(t,origin,available){
  const url=new URL("/admin/",origin);
  url.hash="tables?"+new URLSearchParams({s:t.schema,t:t.table});
  const a=link(t.schema+"."+t.table,url.href);
  a.target="_blank";a.rel="noopener";a.className="catalog-table-link";
  const part=node("span");part.append(a,document.createTextNode("（"+t.name+"）"));
  if(t.fieldPath)part.append(node("code"," · "+t.fieldPath));
  if(available && !available.has(t.schema+"."+t.table))part.append(node("small"," 当前库暂无此表"));
  return part;
}
const parameterNames={
 symbol:"证券代码",peerSymbol:"对比证券代码",version:"发布版本",view:"返回视图",news:"新闻读取模式",
 cursor:"分页游标",limit:"每页条数",page:"页码",pageSize:"每页条数",filters:"筛选条件",
 sortRules:"排序规则",sortMode:"排序模式",bank:"银行评分选项","if-none-match":"客户端缓存版本",
 query:"搜索关键词",q:"搜索关键词",field:"排序字段",direction:"排序方向"
};
function schemaRows(schema,prefix="",required=false,trail=[]){
  if(!schema)return [];
  let type=schema.type||schema.$ref?.split("/").at(-1)||"object";
  if(schema.anyOf || schema.oneOf)type=(schema.anyOf||schema.oneOf).map(s=>s.type||s.$ref?.split("/").at(-1)||"object").join(" / ");
  const bounds=["default","minimum","maximum","minLength","maxLength","maxItems","minItems","enum","pattern"]
    .filter(k=>Object.hasOwn(schema,k)).map(k=>k+"="+JSON.stringify(schema[k])).join("，");
  const last=prefix.split(".").at(-1);
  const label=parameterNames[last]||schema.title||"参数含义待核验";
  const rows=prefix?[[prefix,label,required?"是":"否",type,bounds||schema.description||"—"]]:[];
  for(const [key,value]of Object.entries(schema.properties||{})){
    rows.push(...schemaRows(value,prefix?prefix+"."+key:key,(schema.required||[]).includes(key),trail));
  }
  if(schema.items)rows.push(...schemaRows(schema.items,prefix+"[]",false,trail));
  for(const alternative of [...(schema.anyOf||[]),...(schema.oneOf||[]),...(schema.allOf||[])]){
    if(alternative.type==="object" || alternative.properties)rows.push(...schemaRows(alternative,prefix,false,trail));
  }
  return rows;
}
function operation(box,op,origin){
  box.append(node("h3",op.kind==="independent"?"独立查询接口":"聚合获取接口"),
    node("pre",op.method+" "+op.path,"catalog-call"));
  for(const path of op.responsePaths||[])box.append(node("p",path,"mono"));
  if(!op.available){box.append(node("p","目录待更新：该路由当前不存在","catalog-warning"));return;}
  const rows=[];
  for(const p of op.parameters){
    const schema=p.schema||{};
    rows.push([p.name,parameterNames[p.name]||p.description||"参数含义待核验",p.required?"是":"否",schema.type||"联合类型",
      ["default","minimum","maximum","maxLength","minLength","enum"].filter(k=>Object.hasOwn(schema,k)).map(k=>k+"="+JSON.stringify(schema[k])).join("，")||"—"]);
  }
  if(op.requestBody)rows.push(...schemaRows(op.requestBody.schema));
  if(rows.length)box.append(table(["参数","中文含义","必填","类型","默认值及约束"],rows));
  else box.append(node("p","无需请求参数。","muted"));
  if(op.method==="GET"){
    const url=new URL(op.path.replace("{symbol}","600519.SH"),origin);
    if(op.path==="/api/v1/search")url.searchParams.set("q","600519");
    box.append(node("pre",'curl "'+url.href+'"',"catalog-call"));
  }
}
let indexPromise;
export async function renderCatalog(container,params,{api,origin}){
  indexPromise ||= api("/admin/api/catalog").catch(error=>{indexPromise=null;throw error;});
  const catalog=await indexPromise;
  const requested=params.get("dataset");
  const id=requested||"stock-daily";
  const root=node("div",null,"catalog-layout"),sidebar=node("aside",null,"catalog-sidebar"),body=node("article",null,"catalog-body");
  const search=node("input");search.type="search";search.placeholder="搜索数据、字段、来源或表";search.setAttribute("aria-label","搜索数据目录");
  search.value=params.get("q")||"";
  const navigation=node("div");
  sidebar.append(search,navigation);
  root.append(sidebar,body);container.append(root);
  const current=()=>location.hash.startsWith("#docs") && (new URLSearchParams(location.hash.split("?")[1]||"").get("dataset")||"stock-daily")===id;
  function destination(entry,field){
    const p=new URLSearchParams({dataset:entry.id});
    if(search.value)p.set("q",search.value);
    if(field)p.set("field",field);
    return "#docs?"+p;
  }
  function drawNavigation(){
    navigation.replaceChildren();
    const query=search.value.trim().toLowerCase();
    let total=0;
    for(const category of catalog.categories){
      const matches=catalog.entries.filter(e=>e.category===category.id&&(!query||e.search.toLowerCase().includes(query)));
      if(!matches.length)continue;
      const group=node("details");group.open=!!query||matches.some(e=>e.id===id);
      group.append(node("summary",category.name+" · "+matches.length));
      for(const entry of matches){
        const matched=entry.fields.find(f=>query&&(f.id+" "+f.name).toLowerCase().includes(query));
        const a=link(entry.name,destination(entry,matched?.id));a.className="catalog-entry";
        if(entry.id===id)a.setAttribute("aria-current","page");
        if(matched)a.append(node("small",matched.id+"（"+matched.name+"）"));
        group.append(a);
      }
      navigation.append(group);total+=matches.length;
    }
    if(!total)navigation.append(node("p","没有匹配的数据条目","muted"));
  }
  search.addEventListener("input",()=>{
    const p=new URLSearchParams(location.hash.split("?")[1]||"");
    if(search.value)p.set("q",search.value);else p.delete("q");
    history.replaceState(null,"","#docs?"+p);
    drawNavigation();
  });
  drawNavigation();
  let result;
  try{result=await api("/admin/api/catalog/"+encodeURIComponent(id));}
  catch(error){body.append(node("p","无法打开数据条目："+id+"。"+error.message,"catalog-warning"));return;}
  const e=result.entry;
  const runtimeLabels=[];
  const tableLabels=[];
  const metadata=node("dl",null,"catalog-meta");
  const addMeta=(label,value)=>{
    metadata.append(node("dt",label));
    const dd=node("dd");
    if(value instanceof Node)dd.append(value);else dd.textContent=value;
    metadata.append(dd);
    return dd;
  };
  body.append(node("p",catalog.categories.find(c=>c.id===e.category)?.name||e.category,"muted"),
    node("h2",e.name,"catalog-title"),node("p",e.description),node("p",e.status,"muted"));
  addMeta("内容标识",e.id);
  addMeta("覆盖范围",e.scope);
  addMeta("数据来源",e.sources.length?e.sources.map(s=>s.name+" · "+(s.api||"")+" · "+s.kind+" · "+s.description).join("\n"):"来源见输入表与下方依据，具体发布方待逐项核验");
  const frequency=node("div");
  for(const f of e.frequencies){
    frequency.append(node("p",f.name+" · "+f.description));
    if(f.task){const state=node("p","运行方信息读取中…","muted");frequency.append(state);runtimeLabels.push({task:f.task,state});}
    if(f.config)frequency.append(node("code",f.config));
  }
  addMeta("获取频率",frequency);
  const tableList=node("div",null,"catalog-tables");
  for(const t of e.tables){
    const place=node("p");place.append(refLink(t,origin));tableList.append(place);tableLabels.push({t,place});
  }
  addMeta("对应数据表",tableList);
  addMeta("保存历期数据",judgment(e.history.records));
  addMeta("保留每次修订／重算",judgment(e.history.revisions));
  addMeta("接口查询历史",judgment(e.history.query));
  addMeta("派生指标",judgment(e.derived));
  addMeta("独立查询接口",e.access.some(a=>a.kind==="independent")?"是。调用方式见下方。":"否。"+(e.access.some(a=>a.path)?"通过下方聚合接口或数据表获取。":"目前通过对应数据表查看。"));
  body.append(metadata);
  const jump=node("div",null,"catalog-jumps");
  for(const [target,label]of [["access","获取方式"],["fields","字段说明"],["calculations","计算口径"],["evidence","核对依据"]]){
    const b=node("button",label);
    b.onclick=()=>body.querySelector('[data-section="'+target+'"]')?.scrollIntoView({block:"start",behavior:"smooth"});
    jump.append(b);
  }
  body.append(jump);
  function section(key,label){
    const box=node("section");box.dataset.section=key;heading(box,label);body.append(box);return box;
  }
  const calls=section("access","获取方式");
  for(const a of e.access){
    if(a.description)calls.append(node("p",a.description));
  }
  for(const op of result.operations)operation(calls,op,origin);
  if(!result.operations.length)calls.append(node("p","此内容当前没有已核准的公共查询路径，可通过对应数据表核对。"));
  calls.append(link("Swagger UI","/docs"),document.createTextNode(" · "),link("OpenAPI JSON","/openapi.json"));
  const fields=section("fields","字段说明");
  const roles={source:"上游原字段",storage:"存储字段",response:"接口返回字段",input:"计算输入"};
  const fieldRows=e.fields.map(f=>{
    const code=node("code",f.id);
    if(params.get("field")===f.id)code.className="catalog-match";
    const mapped=node("div");
    f.tableRefs.forEach(t=>{const p=node("p");p.append(refLink(t,origin));mapped.append(p);});
    return [code,f.name,roles[f.role],f.type,f.unit,f.nullable,judgment(f.derived),mapped,f.description];
  });
  fields.append(table(["英文字段","中文含义","角色","类型","单位","空值","派生","表字段","说明"],fieldRows));
  const calculations=section("calculations","计算口径");
  if(!e.calculations.length)calculations.append(node("p",e.derived.value===null?
    "计算口径待核验。具体缺口与当前依据见字段说明。":"暂无底座派生公式。来源原始指标的定义见字段及来源说明。","muted"));
  else{
    const selected=params.get("formula")||e.calculations[0].id;
    const controls=node("div",null,"catalog-formulas"),formulaBox=node("div",null,"catalog-formula");
    const show=c=>{
      formulaBox.replaceChildren(node("h3",c.name),node("p",c.formula,"catalog-expression"),
        node("p","公式版本 · "+c.version),node("p","统计窗口 · "+c.window),node("p","缺失与冲突 · "+c.missing),
        table(["输入标识","中文含义"],c.inputs.map(i=>[node("code",i.id),i.name])));
      controls.querySelectorAll("button").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.id===c.id)));
    };
    for(const c of e.calculations){
      const b=node("button",c.name);b.dataset.id=c.id;
      b.onclick=()=>{
        const p=new URLSearchParams(location.hash.split("?")[1]||"");p.set("formula",c.id);
        history.replaceState(null,"","#docs?"+p);show(c);
      };controls.append(b);
    }
    calculations.append(controls,formulaBox);
    show(e.calculations.find(c=>c.id===selected)||e.calculations[0]);
  }
  const evidence=section("evidence","核对依据");
  evidence.append(node("p","目录版本 "+catalog.version+"。旧核验结果的日期以原文为准。","muted"));
  for(const path of e.evidence){
    if(path.startsWith("docs/")){
      const p=node("p");p.append(link(path,"/admin/docs/"+path.slice(5)));evidence.append(p);
    }
    else evidence.append(node("p",path,"mono"));
  }
  for(const issue of result.issues)evidence.append(node("p",issue.path+" · "+issue.message,"catalog-warning"));
  // 附加信息不阻塞正文，跨页后丢弃旧请求的更新。
  api("/admin/api/catalog-runtime").then(runtime=>{
    if(!current())return;
    for(const {task,state}of runtimeLabels){
      const row=runtime.tasks.find(t=>t.name===task);
      state.textContent=row?(row.enabled?"任务已启用":"任务已关闭")+" · 配置规则 "+row.rule+" · "+row.applyState+
        (row.effective?" · 运行方已确认 "+row.effective:" · 实际规则待确认"):"运行方信息待确认";
    }
    if(runtime.tableState==="available"){
      const known=new Set(runtime.tables.map(t=>t.schema+"."+t.name));
      tableLabels.forEach(({t,place})=>place.replaceChildren(refLink(t,origin,known)));
    }
    for(const issue of runtime.issues)evidence.append(node("p",issue,"catalog-warning"));
  }).catch(()=>{
    if(current())runtimeLabels.forEach(({state})=>{state.textContent="运行方信息暂不可用，生效状态待确认";});
  });
}
