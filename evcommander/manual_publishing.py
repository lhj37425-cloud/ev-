"""Keep the publishing workspace layout and replace submission with copy."""

ASSET = "/assets/index-Cjh96X_m.js"

COPY_ACTION = r'''
async function evCopyPublishText(text){
  if(!text.trim()){window.$message?.warning("请先填写要复制的发布文案");return;}
  try{
    await navigator.clipboard.writeText(text);
    window.$message?.success("已复制，请打开对应平台粘贴文案");
  }catch{
    window.prompt("请手动复制下面的文案",text);
  }
}
function evPublishBlock(title,description,tags){
  const hashtags=tags.map(tag=>"#"+tag.replace(/^#+/,"")).join(" ");
  return [title?.trim(),description?.trim(),hashtags].filter(Boolean).join("\n\n");
}
async function on(){
  const platforms=x.value;
  const blocks=platforms.length
    ? platforms.map(key=>({name:ue(key),body:evPublishBlock(tt(key),Mt(key),rt(Gt(key)))}))
    : [{name:"",body:evPublishBlock(D.value,E.value,Re.value)}];
  await evCopyPublishText(blocks.filter(block=>block.body).map(block=>
    blocks.length>1 ? "【"+block.name+"】\n"+block.body : block.body
  ).join("\n\n"));
}
'''

BATCH_COPY = r'''
async function pl(){
  const required=as(Ie.value);
  const drafts=q.value.map(item=>{
    const copy=evPublishBlock(item.title,item.desc,[...new Set([...required,...as(item.tagsText)])]);
    return copy;
  }).filter(Boolean);
  await evCopyPublishText(drafts.join("\n\n————\n\n"));
}
'''


def replace_region(source, start, end, replacement):
    if source.count(start) != 1 or source.count(end) != 1:
        raise RuntimeError("Publishing adapter does not match the archived asset.")
    begin = source.index(start)
    finish = source.index(end, begin + len(start))
    return source[:begin] + replacement + source[finish:]


def prepare_manual_publishing(root):
    source = (root / ASSET[1:]).read_text(encoding="utf-8")
    source = replace_region(source, "async function on(){", "function gt(){", COPY_ACTION)
    source = replace_region(source, "async function pl(){", "function ms(){", BATCH_COPY)
    source = source.replace('onClick:on},r($l.value)', 'onClick:on},"复制发布文案"')
    source = source.replace(
        'disabled:p.value||b.value||_.value||!!oe.value,onClick:pl},r(b.value?"提交中…":_.value?"确认中…":oe.value?"已受理":R.value?"核对受理":"提交批次")',
        'disabled:!q.value.length,onClick:pl},"复制批次文案"',
    )
    source = source.replace('"一键多平台发布"', '"多平台发布"')
    source = source.replace(
        "正文内容（开启右侧开关时发布会自动带「AI 生成」标注）",
        "正文内容（复制后在目标平台粘贴）",
    )
    if 'onClick:on},"复制发布文案"' not in source or 'onClick:pl},"复制批次文案"' not in source:
        raise RuntimeError("Publishing buttons could not be adapted.")
    return {ASSET: source.encode("utf-8")}
