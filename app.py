import os, io, re, json, time, uuid, asyncio, threading, subprocess, secrets, hmac
from pathlib import Path
from http.cookies import SimpleCookie
import fitz
from mutagen.mp3 import MP3
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, JSONResponse
import uvicorn
import edge_tts

ROOT=Path('/app/data');ROOT.mkdir(parents=True,exist_ok=True)
PASSWORD=os.environ.get('CHMURA_PASSWORD','')
SECRET=os.environ.get('CHMURA_SESSION_SECRET','')
if not PASSWORD or not SECRET: raise RuntimeError('Set CHMURA_PASSWORD and CHMURA_SESSION_SECRET in Render')
app=FastAPI(title='Chmura Narratora ZUE')
jobs={}
lock=threading.Lock()
busy=threading.Semaphore(1)
MAX_SIZE=400*1024*1024
VOICE='pl-PL-MarekNeural'
VOICES={'pl-PL-MarekNeural':'Marek — głos polski','pl-PL-ZofiaNeural':'Zofia — głos polski'}

def safe(s): return re.sub(r'[^a-zA-Z0-9_-]','',s)[:80]
def cookie_value(): return hmac.new(SECRET.encode(),PASSWORD.encode(),'sha256').hexdigest()
def authorized(req):
    return hmac.compare_digest(req.cookies.get('chmura',''),cookie_value())
def auth(req):
    if not authorized(req): raise HTTPException(status_code=401,detail='Zaloguj się')
def state(job,step,percent):
    job.update(stage=step,progress=percent)
def ff(args):
    p=subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y']+list(map(str,args)),capture_output=True,text=True)
    if p.returncode: raise RuntimeError(p.stderr[-900:])

def clean_text(page):
    blocks=page.get_text('blocks')
    text='\n'.join(b[4] for b in sorted(blocks,key=lambda b:(b[1],b[0])) if len(b)>4 and isinstance(b[4],str))
    lines=[re.sub(r'\s+',' ',x).strip() for x in text.splitlines()]
    result=[]
    for line in lines:
        if not line:continue
        if re.fullmatch(r'\d{1,4}',line):continue
        if 'NIEKOŃCZĄCA SIĘ KSIĘGA' in line.upper() and len(line)<110:continue
        if 'CZĘŚĆ SIÓDMA' in line.upper() and len(line)<110:continue
        if re.fullmatch(r'ROZDZIAŁ\s+[IVXLCDM\d]+',line,re.I):continue
        if len(line)<70 and line.isupper() and len(result)==0:continue
        result.append(line)
    return ' '.join(result)

async def speak(txt,out,voice=VOICE):
    # Naturalnie brzmiący głos neuronowy online; nie jest żywym lektorem.
    if voice not in VOICES: raise ValueError('Nieznany głos')
    await edge_tts.Communicate(txt,voice,rate='-4%').save(str(out))
    if not out.exists() or out.stat().st_size<1000:raise RuntimeError('Nie udało się uzyskać narracji')
def render(job,path,intro,voice=VOICE,custom_recording=None):
    if not busy.acquire(blocking=False):
        state(job,'Inny wideobook jest właśnie tworzony — spróbuj ponownie później',0);job['error']='Zajęte studio';return
    try:
        wd=ROOT/job['id'];wd.mkdir(exist_ok=True)
        pdf=fitz.open(path)
        total=len(pdf);job['pages']=total
        if total>400: raise RuntimeError('Limit 400 stron na jeden projekt')
        audios=[];slides=[];durations=[];texts=[]
        first_chapter=next((i for i in range(min(total,20)) if re.search(r'ROZDZIAŁ\s+I\b',pdf[i].get_text(),re.I)),min(total-1,5))
        # For the known book, PDF page 2 = short introduction, pages 3-4 = silent table of contents.
        for i in range(total):
            page=pdf[i]
            img=wd/f'{i:04d}.jpg'
            pix=page.get_pixmap(matrix=fitz.Matrix(1.25,1.25),alpha=False)
            pix.save(img,jpg_quality=82)
            slides.append(img)
            if i==0: spoken=''
            elif i==1: spoken=intro.strip() or 'Niekończąca się Księga. Część siódma. Tom drugi. Zapraszamy do świata Zuemy i kosmicznej rasy Zue.'
            elif i<first_chapter: spoken=''
            else: spoken=clean_text(page)
            texts.append(spoken[:5000])
        pdf.close()
        if custom_recording:
            probe=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(custom_recording)],capture_output=True,text=True,timeout=25)
            if probe.returncode: raise RuntimeError('Nie można odczytać czasu nagrania lektora')
            full_duration=float(probe.stdout.strip())
            spoken_total=sum(len(t.split()) for t in texts if t)
            if spoken_total==0: raise RuntimeError('Brak tekstu w PDF')
            pointer=0.0
            last_spoken=max(i for i,t in enumerate(texts) if t)
        # split passages to avoid very large requests
        for i,spoken in enumerate(texts):
            state(job,'Nagrywanie polskiego narratora: strona '+str(i+1)+' z '+str(total),round(10+65*i/total))
            dest=wd/f'a{i:04d}.mp3'
            if spoken and custom_recording:
                # Nagrany lektor czyta całą książkę; czasy stron szacowane długością tekstu.
                part=(full_duration-pointer if i==last_spoken else full_duration*len(spoken.split())/spoken_total)
                part=max(0.05,part)
                ff(['-ss',str(round(pointer,3)),'-t',str(round(part,3)),'-i',custom_recording,
                    '-vn','-ar','24000','-ac','1','-c:a','libmp3lame','-b:a','64k',dest])
                pointer+=part
                dur=max(0.2,MP3(dest).info.length)
            elif spoken:
                pieces=[spoken[j:j+1600] for j in range(0,len(spoken),1600)]
                if len(pieces)==1: asyncio.run(speak(pieces[0],dest,voice))
                else:
                    split=[]
                    for j,piece in enumerate(pieces):
                        p=wd/f'p{i:04d}_{j}.mp3';asyncio.run(speak(piece,p,voice));split.append(p)
                    lst=wd/f'm{i:04d}.txt'
                    lst.write_text(''.join("file '"+p.name+"'\n" for p in split))
                    ff(['-f','concat','-safe','0','-i',lst,'-c','copy',dest])
                dur=max(1.5,MP3(dest).info.length)
            else:
                dur=4 if i in (0,1) else 6
                ff(['-f','lavfi','-i','anullsrc=r=24000:cl=mono','-t',str(dur),'-codec:a','libmp3lame','-b:a','64k',dest])
            audios.append(dest);durations.append(dur)
        state(job,'Łączenie nagrania i obrazu',78)
        audio_list=wd/'audio.txt';audio_list.write_text(''.join("file '"+p.name+"'\n" for p in audios))
        audio=wd/'narracja.mp3'
        ff(['-f','concat','-safe','0','-i',audio_list,'-c','copy',audio])
        slide_list=wd/'images.txt'
        slide_list.write_text(''.join("file '"+slides[i].name+"'\nduration "+str(round(durations[i],3))+"\n" for i in range(total))+"file '"+slides[-1].name+"'\n")
        video=wd/'wideobook.mp4'
        ff(['-f','concat','-safe','0','-i',slide_list,'-i',audio,'-map','0:v:0','-map','1:a:0','-vsync','vfr','-vf','pad=ceil(iw/2)*2:ceil(ih/2)*2:0:0:black','-c:v','libx264','-preset','ultrafast','-crf','29','-pix_fmt','yuv420p','-c:a','aac','-b:a','96k','-movflags','+faststart','-shortest',video])
        state(job,'Gotowy wideobook MP4 i MP3',100)
        job['ready']=True
    except Exception as e:
        job['error']=str(e);state(job,'Błąd: '+str(e),job.get('progress',0))
    finally: busy.release()

@app.get('/api/narrators')
def narrators(req:Request):
    auth(req)
    return VOICES

@app.get('/api/voice-sample/{voice}')
async def voice_sample(req:Request,voice:str):
    auth(req)
    if voice not in VOICES:raise HTTPException(400,'Nieznany głos')
    sample=ROOT/('sample_'+voice+'.mp3')
    if not sample.exists() or sample.stat().st_size<1000:
        try:
            await speak('Witam w Chmurze Narratora Zue. Posłuchaj próbki mojego głosu.',sample,voice)
        except Exception as e:raise HTTPException(502,'Próbka głosu niedostępna: '+str(e))
    return FileResponse(sample,media_type='audio/mpeg')

@app.post('/api/add-narrator')
async def add_narrator(req:Request):
    auth(req)
    ext=req.query_params.get('ext','').lower()
    if ext not in ('mp3','wav','m4a'):raise HTTPException(400,'Wybierz MP3, WAV lub M4A')
    token=uuid.uuid4().hex
    directory=ROOT/'narrators';directory.mkdir(exist_ok=True)
    dest=directory/(token+'.audio')
    size=0
    try:
        with dest.open('wb') as out:
            async for chunk in req.stream():
                size+=len(chunk)
                if size>250*1024*1024:raise HTTPException(413,'Limit nagrania 250 MB')
                out.write(chunk)
        probe=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(dest)],capture_output=True,text=True,timeout=25)
        if probe.returncode or not probe.stdout.strip():raise HTTPException(400,'Niepoprawne nagranie audio')
        duration=float(probe.stdout.strip())
        if duration<3 or duration>12*3600:raise HTTPException(400,'Nagranie musi trwać od 3 sekund do 12 godzin')
        return {'narrator_id':token,'duration_seconds':round(duration,1)}
    except:
        dest.unlink(missing_ok=True)
        raise

HTML=r'''<!DOCTYPE html><html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CHMURA NARRATORA ZUE</title><style>
:root{font-family:system-ui;background:#071b22;color:#e9e4cc}body{margin:0}header{text-align:center;padding:35px 12px;border-bottom:2px solid #ad873a;color:#f8cf6d}h1{font-size:clamp(24px,5vw,44px);margin:0 0 8px}main{max-width:740px;margin:30px auto;padding:0 20px}.panel{border:1px solid #b99954;border-radius:15px;background:#112a31;padding:25px;box-shadow:0 5px 30px #0005}label{display:block;margin:15px 0 7px;font-weight:600}input,textarea,select{box-sizing:border-box;width:100%;font:inherit;background:#f9f5e9;color:#162527;padding:13px;border-radius:8px;border:0}textarea{min-height:95px}button{font:inherit;margin:18px 0 8px;background:linear-gradient(90deg,#f9df88,#bc8a34);color:#14252a;font-weight:800;border:0;border-radius:9px;padding:14px 20px;cursor:pointer;width:100%}p{line-height:1.5}.muted{color:#c3c8c4}.status{min-height:60px;background:#0b2028;border-radius:9px;padding:15px;margin-top:12px}.links a{display:block;margin:14px 0;color:#ffdc82;font-weight:700}progress{width:100%;height:24px}.secondary{background:#294953;color:#ffdd8b;border:1px solid #ac873a}audio{width:100%}select{margin:8px 0}footer{text-align:center;margin:25px;opacity:.65}</style></head><body><header><h1>CHMURA NARRATORA ZUE</h1><div>PDF → NARRACJA → WIDEOBOOK MP4</div></header><main><div class="panel">
<h2>Dodaj całą książkę PDF</h2><p class="muted">Automatyczne tworzenie wideobooków. Głos polskiego narratora Marek. Spis treści bez czytania, następnie rozdziały.</p>
<label>Książka PDF</label><input id="file" type="file" accept=".pdf,application/pdf">
<label>Krótki wstęp czytany przez narratora</label><textarea id="intro">Niekończąca się Księga. Oto kolejny tom podróży Zuemy i kosmicznej rasy Zue. Zapraszamy do wysłuchania opowieści.</textarea>
<label>WYBIERZ LEKTORA DO WIDEOBOOKA</label>
<select id="voice" onchange="chooseVoice()">
<option value="pl-PL-MarekNeural">Marek — głos neuronowy</option>
<option value="pl-PL-ZofiaNeural">Zofia — głos neuronowy</option>
<option value="custom">Mój lektor — własne nagranie</option>
</select>
<div id="neural"><p class="muted">Wbudowane polskie głosy neuronowe mogą automatycznie przeczytać dowolny przesłany PDF.</p></div>
<details id="narrator-add" style="background:#09232a;border:1px solid #a5843d;border-radius:10px;padding:15px;margin:18px 0">
<summary style="font-size:20px;cursor:pointer;color:#ffd77a;font-weight:800">➕ DODAJ LEKTORA</summary>
<label>Imię lub nazwa nowego lektora</label>
<input id="new-narrator-name" type="text" maxlength="70" placeholder="Np. Narrator ZUE">
<label>Sposób czytania książki</label>
<select id="new-narrator-mode">
<option value="pl-PL-MarekNeural">Automatycznie: polski głos męski</option>
<option value="pl-PL-ZofiaNeural">Automatycznie: polski głos żeński</option>
<option value="custom">Ludzki lektor: wgram całe nagranie książki</option>
</select>
<button type="button" onclick="registerNarrator()">DODAJ LEKTORA DO APLIKACJI</button>
<p id="add-narrator-status" class="muted"></p>
<p class="muted">Dodany lektor pojawi się na liście powyżej. Automatyczne czytanie PDF działa dla głosów neuronowych. Przy lektorze ludzkim potrzebne jest nagranie całej książki, a nie fragment głosu.</p>
</details>
<div id="custom" style="display:none">
<label>Wgraj pełne nagranie wybranego lektora (MP3, WAV lub M4A)</label>
<input id="narrator-file" type="file" accept=".mp3,.wav,.m4a,audio/*">
<button class="secondary" type="button" onclick="addNarrator()">WGRAJ NAGRANIE KSIĄŻKI</button>
<p id="narrator-result" class="muted"></p>
<p class="muted"><b>Ważne:</b> wgraj pełny odczyt książki: wstęp i rozdziały bez spisu treści. Nagranie zostanie połączone ze stronami PDF, których czas jest dopasowywany orientacyjnie.</p>
</div>
<button id="create" onclick="upload()">UTWÓRZ WIDEOBOOK MP4</button>
<progress id="bar" max="100" value="0"></progress><div id="status" class="status">Wybierz PDF i naciśnij UTWÓRZ WIDEOBOOK.</div><div class="links" id="links"></div>
<p class="muted">Na darmowym serwerze generowanie bardzo długich książek może się nie udać z powodu ograniczonej pamięci i czasu. Gotowe pliki pobierz od razu: pamięć serwera nie jest trwała.</p>
</div></main><footer>Andrzej Muller · Zuema · Studio wideobooków — bez sklepu i płatności</footer><script>
let uploadedNarrator='';
const storageKey='chmura-zue-narrators-v1';
let savedNarrators=[];
try{savedNarrators=JSON.parse(localStorage.getItem(storageKey)||'[]');if(!Array.isArray(savedNarrators))savedNarrators=[]}catch(e){savedNarrators=[]}
function getMode(){
 const selected=document.getElementById('voice').value;
 const entry=savedNarrators.find(x=>x.id===selected);
 return entry?entry.mode:selected;
}
function chooseVoice(){
 const isCustom=getMode()==='custom';
 document.getElementById('custom').style.display=isCustom?'block':'none';
 document.getElementById('neural').style.display=isCustom?'none':'block';
}
function registerNarrator(){
 const name=document.getElementById('new-narrator-name').value.trim();
 const mode=document.getElementById('new-narrator-mode').value;
 const status=document.getElementById('add-narrator-status');
 if(name.length<2){status.textContent='Wpisz imię lub nazwę lektora.';return}
 if(savedNarrators.some(x=>x.name.toLocaleLowerCase('pl')===name.toLocaleLowerCase('pl'))){status.textContent='Lektor o takiej nazwie już istnieje.';return}
 const entry={id:'n-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,8),name:name,mode:mode};
 savedNarrators.push(entry);
 try{localStorage.setItem(storageKey,JSON.stringify(savedNarrators))}catch(e){}
 const opt=new Option(name+(mode==='custom'?' — nagranie ludzkie':' — czyta PDF automatycznie'),entry.id);
 document.getElementById('voice').add(opt);
 document.getElementById('voice').value=entry.id;
 chooseVoice();
 status.textContent='Dodano lektora: '+name+'. Wybierz PDF i rozpocznij tworzenie wideobooka.';
 document.getElementById('narrator-add').open=false;
}
function initNarrators(){
 const selector=document.getElementById('voice');
 for(const item of savedNarrators){
  if(item&&typeof item.name==='string'&&['pl-PL-MarekNeural','pl-PL-ZofiaNeural','custom'].includes(item.mode)){
   selector.add(new Option(item.name+(item.mode==='custom'?' — nagranie ludzkie':' — czyta PDF automatycznie'),item.id));
  }
 }
 chooseVoice();
}
initNarrators();
async function addNarrator(){let f=document.getElementById('narrator-file').files[0],label=document.getElementById('narrator-result');uploadedNarrator='';if(!f){label.textContent='Wybierz plik nagrania lektora';return}if(f.size>250*1024*1024){label.textContent='Nagranie przekracza 250 MB';return}
let ext=f.name.split('.').pop().toLowerCase();label.textContent='Wgrywanie nagrania lektora...';
try{let r=await fetch('/api/add-narrator?ext='+encodeURIComponent(ext),{method:'POST',body:f,headers:{'Content-Type':'application/octet-stream'}});let d=await r.json();if(!r.ok)throw Error(d.detail||'Błąd nagrania');uploadedNarrator=d.narrator_id;label.textContent='LEKTOR DODANY — nagranie: '+Math.round(d.duration_seconds/60)+' min. Możesz tworzyć wideobook.'}catch(e){label.textContent=e.message}}
async function upload(){let f=document.getElementById('file').files[0];if(!f){alert('Najpierw wybierz PDF');return}if(f.size>400*1024*1024){alert('Limit pliku PDF: 400 MB');return}
let selectedVoice=getMode();
if(selectedVoice==='custom'&&!uploadedNarrator){alert('Najpierw kliknij DODAJ LEKTORA i wgraj całe nagranie książki');return}
let btn=document.getElementById('create');btn.disabled=true;let s=document.getElementById('status');s.textContent='Przesyłanie całego PDF...';try{
let res=await fetch('/api/upload?intro='+encodeURIComponent(document.getElementById('intro').value)+'&voice='+encodeURIComponent(selectedVoice==='custom'?'pl-PL-MarekNeural':selectedVoice)+'&narrator='+encodeURIComponent(selectedVoice==='custom'?uploadedNarrator:''),{method:'POST',body:f,headers:{'Content-Type':'application/pdf'}});let data=await res.json();if(!res.ok)throw Error(data.detail||'Błąd przesyłania');let id=data.id;
let timer=setInterval(async()=>{try{let x=await (await fetch('/api/status/'+id)).json();document.getElementById('bar').value=x.progress||0;s.textContent=x.stage||'Przetwarzanie';if(x.ready||x.error){clearInterval(timer);btn.disabled=false;if(x.ready)document.getElementById('links').innerHTML='<a href="/api/download/'+id+'/mp4">POBIERZ WIDEOBOOK MP4</a><a href="/api/download/'+id+'/mp3">POBIERZ NARRACJĘ MP3</a>'}}catch(e){s.textContent='Nie można odczytać postępu: '+e.message}},2500)
}catch(e){s.textContent='Błąd: '+e.message;btn.disabled=false}}
</script></body></html>'''
LOGIN='''<!doctype html><html lang="pl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><body style="background:#09222b;color:#f7db88;font:18px system-ui;max-width:480px;margin:13vh auto;padding:24px"><h1>CHMURA NARRATORA ZUE</h1><form method="post" action="/login"><label>Hasło właściciela</label><input type="password" name="password" required style="box-sizing:border-box;width:100%;padding:14px;font-size:20px"><button style="width:100%;padding:14px;margin-top:15px;background:#f4cc76">OTWÓRZ STUDIO</button></form></body></html>'''

@app.get('/healthz')
def health():return {'ok':True}
@app.get('/',response_class=HTMLResponse)
def home(req:Request):
    if not authorized(req):return HTMLResponse(LOGIN)
    return HTMLResponse(HTML)
@app.post('/login')
async def login(req:Request):
    from urllib.parse import parse_qs
    body=(await req.body()).decode()
    password=parse_qs(body).get('password',[''])[0]
    if not hmac.compare_digest(password,PASSWORD): return HTMLResponse('<h2>Niepoprawne hasło</h2><a href="/">Powrót</a>',status_code=401)
    resp=RedirectResponse('/',status_code=303)
    resp.set_cookie('chmura',cookie_value(),httponly=True,secure=True,samesite='lax',max_age=86400)
    return resp
@app.post('/api/upload')
async def upload(req:Request,intro:str='',voice:str=VOICE,narrator:str=''):
    auth(req)
    if voice not in VOICES:raise HTTPException(400,'Nieznany głos narratora')
    custom_path=None
    if narrator:
        if not re.fullmatch('[a-f0-9]{32}',narrator):raise HTTPException(400,'Niepoprawny identyfikator lektora')
        custom_path=ROOT/'narrators'/(narrator+'.audio')
        if not custom_path.is_file():raise HTTPException(400,'Brak pliku lektora')
    if req.headers.get('content-type')!='application/pdf':raise HTTPException(400,'Dodaj plik PDF')
    name=uuid.uuid4().hex
    project=ROOT/name;project.mkdir()
    path=project/'book.pdf'
    n=0
    with path.open('wb') as f:
        async for data in req.stream():
            n+=len(data)
            if n>MAX_SIZE:
                path.unlink(missing_ok=True)
                raise HTTPException(413,'PDF jest za duży')
            f.write(data)
    try:
        pdf=fitz.open(path);count=len(pdf);pdf.close()
        if not count:raise ValueError('Pusty PDF')
    except Exception as e:
        path.unlink(missing_ok=True);raise HTTPException(400,'Niepoprawny PDF: '+str(e))
    job={'id':name,'stage':'Przygotowanie książki','progress':0,'pages':count,'ready':False}
    jobs[name]=job
    threading.Thread(target=render,args=(job,path,intro[:700],voice,custom_path),daemon=True).start()
    return {'id':name,'pages':count}
@app.get('/api/status/{job_id}')
def status(req:Request,job_id:str):
    auth(req)
    if job_id not in jobs:raise HTTPException(404,'Nie znaleziono projektu')
    return jobs[job_id]
@app.get('/api/download/{job_id}/{kind}')
def download(req:Request,job_id:str,kind:str):
    auth(req)
    if job_id not in jobs or not jobs[job_id].get('ready'):raise HTTPException(404,'Plik nie jest gotowy')
    if kind not in ('mp3','mp4'):raise HTTPException(404)
    p=ROOT/job_id/('narracja.mp3' if kind=='mp3' else 'wideobook.mp4')
    return FileResponse(p,filename='Chmura_Narratora_ZUE_'+job_id+'.'+kind,media_type='audio/mpeg' if kind=='mp3' else 'video/mp4')
if __name__=='__main__':uvicorn.run(app,host='0.0.0.0',port=int(os.environ.get('PORT','10000')))
