"""Local Windows capture agent (runs on user PC, not remote server).

Captures microphone + WASAPI loopback, writes synchronized source WAVs and a
mixed WAV, then uploads to TeamMeetingAI `/meetings/multi-source`.

Usage:
  python tools/windows_capture_agent.py --server http://HOST:8000 --host-token ... --user-id ... --title "Meet"
"""
from __future__ import annotations

import argparse, json, threading, time, wave
from pathlib import Path
import numpy as np
import requests

from audio.microphone import MicrophoneCapture, list_microphones
from audio.system_audio import SystemAudioCapture, list_loopback_devices


class WavWriter:
    def __init__(self, path: Path, sr: int, ch: int):
        self.path = path; self.sr=sr; self.ch=ch; self.n=0
        self.w = wave.open(str(path), 'wb'); self.w.setnchannels(ch); self.w.setsampwidth(2); self.w.setframerate(sr)
        self.lock = threading.Lock()
    def write(self, data):
        x = np.asarray(data, dtype=np.float32)
        if x.ndim==1: x=x[:,None]
        if x.shape[1]!=self.ch:
            x = x.mean(axis=1,keepdims=True) if self.ch==1 else np.repeat(x[:,:1], self.ch, axis=1)
        pcm = (np.clip(x,-1,1)*32767).astype('<i2')
        with self.lock:
            self.w.writeframes(pcm.tobytes()); self.n += pcm.shape[0]
    def close(self):
        with self.lock:
            self.w.close()


def mix(mic: Path, sysa: Path, out: Path):
    def read(p):
        with wave.open(str(p),'rb') as w:
            sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
            a=np.frombuffer(w.readframes(n),dtype='<i2').astype(np.float32)
            if ch>1: a=a.reshape(-1,ch).mean(axis=1)
            return sr,a
    sr1,a1=read(mic); sr2,a2=read(sysa)
    sr=sr1
    if sr2!=sr:
        x_old=np.linspace(0,1,len(a2),endpoint=False); x_new=np.linspace(0,1,int(len(a2)*sr/sr2),endpoint=False)
        a2=np.interp(x_new,x_old,a2).astype(np.float32)
    n=max(len(a1),len(a2)); m=np.zeros(n,dtype=np.float32)
    m[:len(a1)]+=a1; m[:len(a2)]+=a2
    m=np.clip(m,-32768,32767).astype('<i2')
    with wave.open(str(out),'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(m.tobytes())


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--server', required=True)
    ap.add_argument('--user-id', required=True)
    ap.add_argument('--title', required=True)
    ap.add_argument('--mic-id', default=None); ap.add_argument('--system-id', default=None)
    ap.add_argument('--seconds', type=int, default=0)
    ap.add_argument('--out', default='capture_out')
    args=ap.parse_args()
    out=Path(args.out); out.mkdir(parents=True, exist_ok=True)
    mic_wav=out/'microphone.wav'; sys_wav=out/'system.wav'; mix_wav=out/'mixed.wav'
    mic_writer=WavWriter(mic_wav,48000,1); sys_writer=WavWriter(sys_wav,48000,2)
    mic=MicrophoneCapture(device_id=args.mic_id, sample_rate=48000, channels=1)
    sysc=SystemAudioCapture(device_id=args.system_id, sample_rate=48000, channels=2)
    mic.start(mic_writer.write, lambda m: print('MIC ERR',m))
    sysc.start(sys_writer.write, lambda m: print('SYS ERR',m))
    print('Recording... Ctrl+C to stop')
    t0=time.time()
    try:
        while True:
            time.sleep(0.5)
            if args.seconds and (time.time()-t0)>=args.seconds: break
    except KeyboardInterrupt:
        pass
    mic.stop(); sysc.stop(); mic_writer.close(); sys_writer.close()
    mix(mic_wav, sys_wav, mix_wav)
    files={'microphone': open(mic_wav,'rb'),'system': open(sys_wav,'rb'),'mixed': open(mix_wav,'rb')}
    data={'title':args.title,'user_id':args.user_id,'duration_ms':str(int((time.time()-t0)*1000)),
          'source_manifest': json.dumps({'microphone':{'source':'wasapi_mic'},'system':{'source':'wasapi_loopback'}})}
    r=requests.post(args.server.rstrip('/')+'/meetings/multi-source', headers={}, data=data, files=files, timeout=3600)
    print(r.status_code, r.text[:500])

if __name__=='__main__':
    main()
