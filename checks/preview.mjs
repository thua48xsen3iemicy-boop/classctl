import {PreviewReceiver} from '../contracts/preview/receiver.mjs';

export async function exercise(events, status) {
  const [subscribe, ready, frame, ack, verified] = events;
  let screen, receipt;
  const receiver = new PreviewReceiver({
    clear:() => {screen = null;}, paint:image => {screen = image;}, ack:value => {receipt = value;}
  });
  const check = (value, name) => {if (!value) throw Error(name);};
  const bytes = new Uint8Array(frame.byteLength);
  const decode = async () => 'current';
  check(receiver.subscribe(subscribe,status), 'valid monitor');
  check(!await receiver.frame(frame,bytes,decode), 'before ready');
  check(receiver.accept(ready), 'ready');
  check(!receiver.accept(verified), 'confirmation without frame');
  check(!await receiver.frame(frame,bytes.subarray(1),decode), 'truncated JPEG');
  check(await receiver.frame(frame,bytes,decode), 'paint');
  check(screen === 'current' && Object.keys(ack).every(k=>ack[k]===receipt[k]), 'ack after paint');
  check(receiver.accept(verified), 'static confirmation');
  check(!await receiver.frame(frame,bytes,decode), 'duplicate');

  for (const delta of [{sessionId:'login-8'}, {layoutRevision:2}]) {
    receiver.subscribe(subscribe,status);
    receiver.accept(ready);
    let finish;
    const pending = receiver.frame(frame,bytes,()=>new Promise(resolve=>{finish=resolve;}));
    receiver.reset();
    const nextStatus = {...status,session:{...status.session,...delta}};
    check(!receiver.subscribe(subscribe,nextStatus), 'reject obsolete subscription');
    check(receiver.subscribe({...subscribe,...delta,subscriptionId:'sub-2'},nextStatus), 'resubscribe');
    receiver.accept({...ready,...delta,subscriptionId:'sub-2'});
    finish('obsolete');
    check(!await pending && screen===null, 'late decode');
    check(!await receiver.frame(frame,bytes,decode) && !receiver.accept(verified), 'late message');
  }
}
