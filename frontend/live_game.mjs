export const MAX_PENDING_HOPS = 2;

export class HopBuffer {
 constructor(){this.reset(0);}
 reset(seq){this.next=seq;this.pending=[];}
 acknowledge(seq,now){
  let rtt=0;
  this.pending=this.pending.filter(hop=>{
   if(hop.seq>=seq)return true;
   rtt=now-hop.sent;
   return false;
  });
  this.next=Math.max(this.next,seq);
  return rtt;
 }
 take(now){
  if(this.pending.length>=MAX_PENDING_HOPS)return null;
  const hop={seq:this.next++,sent:now};
  this.pending.push(hop);
  return hop.seq;
 }
 get size(){return this.pending.length;}
}

export function liveGame({url,authentication,giveaway,difficulty,onState,onInterrupted,onError,onFallback}){
 const buffer=new HopBuffer();
 let socket,handshake,retry,responseTimer,connected=false,ready=false,stopped=false,syncSent=null;
 let failures=0,settleStart;
 const now=()=>performance.now();
 const send=message=>socket.send(JSON.stringify(message));
 const waitForState=()=>{responseTimer??=setTimeout(()=>socket.close(),8000);};
 const clearWait=()=>{clearTimeout(responseTimer);responseTimer=null;};
 const resolveStart=value=>{settleStart?.(value);settleStart=null;};

 function connect(action){
  let candidate;
  try{candidate=new WebSocket(url);}
  catch{
   if(!connected)resolveStart(false);else retryConnection();
   return;
  }
  socket=candidate;ready=false;
  handshake=setTimeout(()=>candidate.close(),1800);
  candidate.onopen=()=>{if(candidate===socket&&!stopped)send({action:'hello',...authentication});};
  candidate.onmessage=({data})=>{
   if(candidate!==socket||stopped)return;
   const event=JSON.parse(data);
   if(event.type==='ready'){
    clearTimeout(handshake);connected=true;
    resolveStart(true);
    send({action,giveaway,...(action==='start'&&difficulty!==undefined?{difficulty}:{})});
    waitForState();
    return;
   }
   if(event.type==='error'){
    ready=false;
    if(event.status===429||event.status>=500){candidate.close();return;}
    close();onError(event.message);
    return;
   }
   if(event.type!=='state')return;
   clearWait();
   const state=event.state,time=now();
   if(event.action==='start'&&state.transport==='http'){
    close();onFallback();return;
   }
   let latency=0;
   if(event.action==='move')latency=buffer.acknowledge(state.seq,time);
   if(event.action==='sync'){latency=syncSent===null?0:time-syncSent;syncSent=null;}
   if(event.action==='start'||event.action==='resume'||state.status!=='playing'){
    buffer.reset(state.seq);
   }
   if(buffer.size||syncSent!==null)waitForState();
   ready=true;failures=0;
   onState(state,latency,event.action,buffer.size);
   if(state.status!=='playing'&&state.registration!=='pending')close();
  };
  candidate.onclose=()=>{
   if(candidate!==socket||stopped)return;
   clearTimeout(handshake);clearWait();ready=false;syncSent=null;buffer.reset(0);
   if(!connected){resolveStart(false);return;}
   retryConnection();
  };
 }

 function retryConnection(){
  if(stopped)return;
  onInterrupted();
  retry=setTimeout(()=>connect('resume'),Math.min(2000,250*2**Math.min(failures++,3)));
 }

 function close(){
  if(stopped)return;
  stopped=true;ready=false;
  clearTimeout(handshake);clearTimeout(retry);clearWait();
  resolveStart(false);
  socket?.close();
 }

 return {
  start:()=>new Promise(resolve=>{
   if(stopped){resolve(false);return;}
   settleStart=resolve;connect('start');
  }),
  jump(){
   if(!ready||socket.readyState!==WebSocket.OPEN||socket.bufferedAmount>1024)return null;
   const seq=buffer.take(now());
   if(seq===null)return null;
   send({action:'move',seq});waitForState();
   return buffer.size;
  },
  sync(){
   if(!ready||buffer.size||syncSent!==null||socket.readyState!==WebSocket.OPEN)return;
   syncSent=now();send({action:'sync'});waitForState();
  },
  close,
 };
}
