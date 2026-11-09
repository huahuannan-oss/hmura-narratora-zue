package pl.humanuniverse.aima;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.media.AudioManager;
import android.net.Uri;
import android.os.Bundle;
import android.print.PrintAttributes;
import android.print.PrintManager;
import android.speech.RecognizerIntent;
import android.speech.tts.TextToSpeech;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {
    private static final int RECOGNIZE = 101, MIC = 102;
    private WebView web;
    private TextToSpeech tts;
    private boolean ttsReady = false;
    private PermissionRequest pendingPermission;
    private final ExecutorService pool = Executors.newCachedThreadPool();

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        setVolumeControlStream(AudioManager.STREAM_MUSIC);
        web = new WebView(this);
        web.setBackgroundColor(0xff060402);
        web.getSettings().setJavaScriptEnabled(true);
        web.getSettings().setDomStorageEnabled(true);
        web.getSettings().setAllowFileAccess(false);
        web.getSettings().setAllowContentAccess(false);
        web.getSettings().setMediaPlaybackRequiresUserGesture(false);
        web.addJavascriptInterface(new Bridge(), "AimaAndroid");
        web.setWebViewClient(new WebViewClient(){
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (request.isForMainFrame() && ("https".equals(uri.getScheme()) || "http".equals(uri.getScheme()))
                    && !"appassets.androidplatform.net".equals(uri.getHost())) {
                    startActivity(new Intent(Intent.ACTION_VIEW, uri));
                    return true;
                }
                return false;
            }
            @Override public void onPageFinished(WebView view, String url) {
                installBridge();
            }
        });
        web.setWebChromeClient(new WebChromeClient(){
            @Override public void onPermissionRequest(PermissionRequest req) {
                runOnUiThread(() -> {
                    if (!"appassets.androidplatform.net".equals(req.getOrigin().getHost())) {req.deny();return;}
                    boolean audio = false;
                    for(String item:req.getResources()) if(PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(item)) audio=true;
                    if(!audio){req.deny();return;}
                    if(checkSelfPermission(Manifest.permission.RECORD_AUDIO)==PackageManager.PERMISSION_GRANTED)
                        req.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
                    else {pendingPermission=req;requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO},MIC);}
                });
            }
        });
        setContentView(web);
        tts = new TextToSpeech(this, status -> {
            if(status==TextToSpeech.SUCCESS && tts!=null)
                ttsReady = tts.setLanguage(Locale.forLanguageTag("pl-PL"))>=TextToSpeech.LANG_AVAILABLE;
        });
        try(InputStream in=getAssets().open("index.html")) {
            String page = new String(in.readAllBytes(),StandardCharsets.UTF_8);
            web.loadDataWithBaseURL("https://appassets.androidplatform.net/index.html",page,"text/html","UTF-8",null);
        } catch(Exception e) {
            web.loadData("<h2>AIMA: brak strony aplikacji</h2>","text/html","UTF-8");
        }
    }

    private void javascript(String command) {
        runOnUiThread(() -> {if(web!=null)web.evaluateJavascript(command,null);});
    }

    private void installBridge(){
        javascript("""
            (function(){
              if(window.__aimaInstalled)return;window.__aimaInstalled=true;
              const normalFetch=window.fetch.bind(window),pending=new Map();let next=0;
              window.fetch=function(resource,opts){
                const url=typeof resource==='string'?resource:resource.url;
                if(!url.startsWith('https://generativelanguage.googleapis.com/v1beta/'))
                  return normalFetch(resource,opts);
                opts=opts||{};
                return new Promise((resolve,reject)=>{
                  const id=++next;pending.set(id,{resolve,reject});
                  let headers=opts.headers||{};
                  if(headers instanceof Headers)headers=Object.fromEntries(headers.entries());
                  AimaAndroid.proxy(JSON.stringify({id:id,url:url,method:opts.method||'GET',headers:headers,body:opts.body||null}));
                });
              };
              window.__aimaProxyResult=function(id,status,body){
                const p=pending.get(id);if(!p)return;pending.delete(id);
                p.resolve(new Response(body,{status:status,headers:{'Content-Type':'application/json'}}));
              };
              window.__aimaRecognized=function(text,error){
                if(error){const s=document.getElementById('status');if(s)s.textContent=error;return;}
                if(!text)return;
                const q=document.getElementById('question');q.value=text;
                const auto=document.getElementById('autoDictation');
                if(auto&&auto.checked){document.getElementById('send').click();}
                else{const panel=document.getElementById('panel');if(panel&&!panel.open)panel.showModal();}
              };
              const mic=document.getElementById('micButton'),listen=document.getElementById('listen');
              if(mic)mic.onclick=function(){AimaAndroid.recognize();};
              if(listen)listen.onclick=function(){AimaAndroid.recognize();};
              if(typeof queueVoice==='function')queueVoice=function(t){AimaAndroid.speak(String(t));};
              const stop=document.getElementById('stopSpeech');
              if(stop)stop.onclick=function(){AimaAndroid.stopSpeech();};
              window.print=function(){AimaAndroid.printPdf();};
            })();
            """);
    }
    public final class Bridge {
        @JavascriptInterface public void recognize(){
            runOnUiThread(()->{
                try{
                    Intent i=new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
                    i.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
                    i.putExtra(RecognizerIntent.EXTRA_LANGUAGE,"pl-PL");
                    i.putExtra(RecognizerIntent.EXTRA_PROMPT,"Powiedz pytanie do AIMA");
                    startActivityForResult(i,RECOGNIZE);
                }catch(Exception e){recognized("","Telefon nie udostępnia rozpoznawania mowy.");}
            });
        }
        @JavascriptInterface public void speak(String text){
            if(text==null || text.isEmpty())return;
            runOnUiThread(()->{if(tts!=null&&ttsReady)tts.speak(text,TextToSpeech.QUEUE_ADD,null,"aima-"+System.nanoTime());});
        }
        @JavascriptInterface public void stopSpeech(){
            runOnUiThread(()->{if(tts!=null)tts.stop();});
        }
        @JavascriptInterface public void printPdf(){
            runOnUiThread(()->{
                PrintManager pm=(PrintManager)getSystemService(PRINT_SERVICE);
                if(pm!=null && web!=null)
                    pm.print("AIMA - debaty PDF",web.createPrintDocumentAdapter("AIMA Debaty"),
                       new PrintAttributes.Builder().setMediaSize(PrintAttributes.MediaSize.ISO_A5).build());
            });
        }
        @JavascriptInterface public void proxy(String payload){
            pool.execute(()->{
                int id=0,status=502;
                String result="{\"error\":{\"message\":\"Błąd połączenia z Gemini\"}}";
                HttpURLConnection c=null;
                try{
                    JSONObject obj=new JSONObject(payload);id=obj.getInt("id");
                    URL url=new URL(obj.getString("url"));
                    if(!"https".equals(url.getProtocol()) ||
                        !"generativelanguage.googleapis.com".equals(url.getHost()) ||
                        !url.getPath().startsWith("/v1beta/"))
                        throw new IllegalArgumentException("Adres API jest niedozwolony");
                    JSONObject headers=obj.optJSONObject("headers");
                    String key=headers==null?"":headers.optString("x-goog-api-key","");
                    if(key.length()<12 || key.length()>300)throw new IllegalArgumentException("Brak klucza Gemini API");
                    c=(HttpURLConnection)url.openConnection();
                    c.setConnectTimeout(20000);c.setReadTimeout(100000);
                    c.setRequestProperty("x-goog-api-key",key);
                    c.setRequestProperty("Accept","application/json");
                    String body=obj.optString("body","");
                    if(!body.isEmpty()){
                        c.setRequestMethod("POST");c.setDoOutput(true);
                        c.setRequestProperty("Content-Type","application/json; charset=utf-8");
                        try(OutputStream out=c.getOutputStream()){out.write(body.getBytes(StandardCharsets.UTF_8));}
                    }
                    status=c.getResponseCode();
                    try(InputStream in=(status>=400)?c.getErrorStream():c.getInputStream()){
                        if(in==null)throw new IllegalStateException("Pusta odpowiedź Gemini");
                        ByteArrayOutputStream b=new ByteArrayOutputStream();
                        byte[] buffer=new byte[8192];int n;
                        while((n=in.read(buffer))!=-1){
                            if(b.size()+n>10_000_000)throw new IllegalStateException("Za duża odpowiedź");
                            b.write(buffer,0,n);
                        }
                        result=b.toString("UTF-8");
                    }
                }catch(Exception e){
                    try{result=new JSONObject().put("error",new JSONObject().put("message",e.getMessage())).toString();}
                    catch(Exception ignored){}
                    status=502;
                }finally{if(c!=null)c.disconnect();}
                int rid=id,rs=status;String body=result;
                javascript("window.__aimaProxyResult("+rid+","+rs+","+JSONObject.quote(body)+");");
            });
        }
    }
    private void recognized(String text,String error){
        javascript("window.__aimaRecognized("+JSONObject.quote(text)+","+JSONObject.quote(error)+");");
    }
    @Override protected void onActivityResult(int code,int result,Intent data){
        super.onActivityResult(code,result,data);
        if(code==RECOGNIZE){
            if(result==RESULT_OK&&data!=null){
                ArrayList<String> xs=data.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS);
                if(xs!=null&&!xs.isEmpty()){recognized(xs.get(0),"");return;}
            }
            recognized("","Nie rozpoznano mowy. Spróbuj ponownie.");
        }
    }
    @Override public void onRequestPermissionsResult(int code,String[] permissions,int[] results){
        super.onRequestPermissionsResult(code,permissions,results);
        if(code==MIC&&pendingPermission!=null){
            if(results.length>0&&results[0]==PackageManager.PERMISSION_GRANTED)
                pendingPermission.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
            else pendingPermission.deny();
            pendingPermission=null;
        }
    }
    @Override protected void onDestroy(){
        pool.shutdownNow();
        if(tts!=null){tts.stop();tts.shutdown();}
        if(web!=null){web.destroy();web=null;}
        super.onDestroy();
    }
}
