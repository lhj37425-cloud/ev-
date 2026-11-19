(()=>{
const previewTarget=document.querySelector('#course-preview');
const scrollToPreview=()=>previewTarget?.scrollIntoView({behavior:'smooth',block:'start'});
document.querySelectorAll('a[href="#course-preview"]').forEach(link=>link.addEventListener('click',event=>{event.preventDefault();history.pushState(null,'','#course-preview');scrollToPreview();}));
if(location.hash==='#course-preview')requestAnimationFrame(scrollToPreview);

// Avoid preloading every clip: concurrent metadata requests can delay playback on slow connections.
const messages={
 zh:{loading:'视频加载中…',slow:'网络较慢，视频仍在加载，请稍候。',blocked:'请点击视频播放按钮继续。',error:'视频加载失败，请重试。',retry:'重试'},
 en:{loading:'Loading video…',slow:'The connection is slow. The video is still loading.',blocked:'Tap the video play button to continue.',error:'The video could not be loaded. Please retry.',retry:'Retry'},
 vi:{loading:'Đang tải video…',slow:'Kết nối chậm. Video vẫn đang tải, vui lòng chờ.',blocked:'Chạm nút phát trên video để tiếp tục.',error:'Không thể tải video. Vui lòng thử lại.',retry:'Thử lại'}
};
const lang=document.documentElement.lang?.slice(0,2)||'vi',text=messages[lang]||messages.vi;
let active=null;
document.querySelectorAll('.mvp-preview-trigger[data-preview-src]').forEach(button=>{
 const duration=button.querySelector('.mvp-preview-duration');
 if(duration?.textContent.trim()==='--:--')duration.hidden=true;
 button.addEventListener('click',()=>{
  if(active)active();
  const container=button.parentElement,video=document.createElement('video'),status=document.createElement('div'),notice=document.createElement('span'),retry=document.createElement('button');
  let slowTimer;
  video.controls=true;video.playsInline=true;video.preload='metadata';video.poster=button.querySelector('img')?.src||'';
  video.setAttribute('aria-label',button.getAttribute('aria-label')||'Video');
  status.className='mvp-preview-status';status.setAttribute('role','status');notice.textContent=text.loading;
  retry.type='button';retry.textContent=text.retry;retry.hidden=true;status.append(notice,retry);container.append(video,status);button.hidden=true;
  const cleanup=()=>{clearTimeout(slowTimer);video.pause();video.removeAttribute('src');video.load();video.remove();status.remove();button.hidden=false;if(active===cleanup)active=null};
  active=cleanup;
  const loading=()=>{retry.hidden=true;notice.textContent=text.loading;status.hidden=false;clearTimeout(slowTimer);slowTimer=setTimeout(()=>{notice.textContent=text.slow},8000)};
  const play=()=>{loading();const promise=video.play();if(promise)promise.catch(()=>{clearTimeout(slowTimer);if(!video.error){notice.textContent=text.blocked;status.hidden=false}})};
  video.addEventListener('playing',()=>{clearTimeout(slowTimer);status.hidden=true});
  video.addEventListener('waiting',loading);
  video.addEventListener('error',()=>{clearTimeout(slowTimer);notice.textContent=text.error;retry.hidden=false;status.hidden=false});
  retry.addEventListener('click',()=>{video.load();play()});
  video.src=button.dataset.previewSrc;
  play();
 });
});
})();
