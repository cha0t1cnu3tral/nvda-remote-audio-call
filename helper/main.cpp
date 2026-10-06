// Copyright (c) 2026 cha0t1cnu3tral. MIT License.
// Standard-user WASAPI capture/playback and Opus; no network access.
#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <audioclientactivationparams.h>
#include <functiondiscoverykeys_devpkey.h>
#include <wrl.h>
#include <wrl/implements.h>
#include <opus.h>
#include <io.h>
#include <fcntl.h>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <deque>
#include <iostream>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

using Microsoft::WRL::ComPtr;
using namespace std::chrono_literals;
constexpr int RATE = 48000, FRAME = 960, MAX_PACKET = 1275;
std::atomic<bool> running{true}, muted{false};
std::atomic<int> volume{100}, started{0};
std::atomic<uint32_t> capturedPackets{0}, decodedPackets{0}, renderedSamples{0}, microphonePeak{0};
std::mutex outputMutex, pcmMutex;
std::deque<int16_t> pcm;

struct Handle {
    HANDLE value;
    explicit Handle(HANDLE v): value(v) { if (!v) throw std::runtime_error("Cannot create event"); }
    ~Handle() { CloseHandle(value); }
};
struct ComScope {
    ComScope() { HRESULT hr=CoInitializeEx(nullptr, COINIT_MULTITHREADED); if(FAILED(hr)) throw std::runtime_error("COM initialization failed"); }
    ~ComScope() { CoUninitialize(); }
};
void check(HRESULT hr, const char* where) {
    if (FAILED(hr)) {
        char msg[160]; std::snprintf(msg, sizeof(msg), "%s (Windows error 0x%08lX)", where, static_cast<unsigned long>(hr));
        throw std::runtime_error(msg);
    }
}
std::string utf8(const std::wstring& s) {
    int n=WideCharToMultiByte(CP_UTF8,0,s.c_str(),static_cast<int>(s.size()),nullptr,0,nullptr,nullptr);
    std::string out(n,' '); WideCharToMultiByte(CP_UTF8,0,s.c_str(),static_cast<int>(s.size()),out.data(),n,nullptr,nullptr);
    std::replace(out.begin(),out.end(),'\t',' '); std::replace(out.begin(),out.end(),'\n',' '); return out;
}
bool readAll(void* p, size_t n) { return std::fread(p,1,n,stdin)==n; }
void sendFrame(uint8_t type, const void* bytes=nullptr, size_t length=0) {
    std::lock_guard<std::mutex> lock(outputMutex);
    uint32_t size=static_cast<uint32_t>(length+1);
    if (std::fwrite(&size,1,4,stdout)!=4 || std::fwrite(&type,1,1,stdout)!=1 || (length && std::fwrite(bytes,1,length,stdout)!=length) || std::fflush(stdout)) running=false;
}
void failure(const std::exception& e) { running=false; std::string s=e.what(); sendFrame(5,s.data(),s.size()); }
ComPtr<IMMDeviceEnumerator> enumerator() {
    ComPtr<IMMDeviceEnumerator> e; check(CoCreateInstance(__uuidof(MMDeviceEnumerator),nullptr,CLSCTX_ALL,IID_PPV_ARGS(&e)),"Audio device enumeration failed"); return e;
}
ComPtr<IAudioClient> deviceClient(EDataFlow flow, const std::wstring& id) {
    auto e=enumerator(); ComPtr<IMMDevice> d;
    if(id==L"default") check(e->GetDefaultAudioEndpoint(flow,eCommunications,&d),"No default audio device");
    else check(e->GetDevice(id.c_str(),&d),"Selected audio device unavailable");
    DWORD state=0; check(d->GetState(&state),"Cannot inspect audio device");
    if(!(state&DEVICE_STATE_ACTIVE)) throw std::runtime_error("Selected audio device is disconnected");
    ComPtr<IAudioClient> c; check(d->Activate(__uuidof(IAudioClient),CLSCTX_ALL,nullptr,reinterpret_cast<void**>(c.GetAddressOf())),"Cannot open audio device"); return c;
}
class Activation final : public Microsoft::WRL::RuntimeClass<Microsoft::WRL::RuntimeClassFlags<Microsoft::WRL::ClassicCom>,Microsoft::WRL::FtmBase,IActivateAudioInterfaceCompletionHandler> {
public:
    Handle event{CreateEventW(nullptr,FALSE,FALSE,nullptr)};
    HRESULT result=E_PENDING;
    ComPtr<IAudioClient> client;
    STDMETHOD(ActivateCompleted)(IActivateAudioInterfaceAsyncOperation* op) override {
        ComPtr<IUnknown> object; HRESULT activation=E_FAIL;
        result=op->GetActivateResult(&activation,&object);
        if(SUCCEEDED(result)) result=activation;
        if(SUCCEEDED(result)) result=object.As(&client);
        SetEvent(event.value); return S_OK;
    }
};
ComPtr<IAudioClient> systemClient(DWORD nvdaPid) {
    AUDIOCLIENT_ACTIVATION_PARAMS args{};
    args.ActivationType=AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
    args.ProcessLoopbackParams.TargetProcessId=nvdaPid;
    args.ProcessLoopbackParams.ProcessLoopbackMode=PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE;
    PROPVARIANT blob{}; blob.vt=VT_BLOB; blob.blob.cbSize=sizeof(args); blob.blob.pBlobData=reinterpret_cast<BYTE*>(&args);
    auto callback=Microsoft::WRL::Make<Activation>(); ComPtr<IActivateAudioInterfaceAsyncOperation> operation;
    check(ActivateAudioInterfaceAsync(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,__uuidof(IAudioClient),&blob,callback.Get(),&operation),"Cannot start capture excluding NVDA; Windows build 20348 or later is required");
    if(WaitForSingleObject(callback->event.value,10000)!=WAIT_OBJECT_0) throw std::runtime_error("Audio capture activation timed out");
    check(callback->result,"Process audio capture unavailable"); return callback->client;
}
WAVEFORMATEX format(int channels) {
    WAVEFORMATEX f{}; f.wFormatTag=WAVE_FORMAT_PCM; f.nChannels=static_cast<WORD>(channels); f.nSamplesPerSec=RATE;
    f.wBitsPerSample=16; f.nBlockAlign=f.nChannels*2; f.nAvgBytesPerSec=RATE*f.nBlockAlign; return f;
}
void requireUserDesktop() {
    HDESK desktop=OpenInputDesktop(0,FALSE,DESKTOP_READOBJECTS);
    if(!desktop) throw std::runtime_error("Audio stopped because Windows switched to a secure or unavailable desktop");
    wchar_t name[128]{}; DWORD needed=0;
    bool normal=GetUserObjectInformationW(desktop,UOI_NAME,name,sizeof(name),&needed) && _wcsicmp(name,L"Default")==0;
    CloseDesktop(desktop);
    if(!normal) throw std::runtime_error("Audio stopped because Windows switched desktops");
}
void capture(const std::wstring& mode, int channels, DWORD pid, const std::wstring& input) {
    try {
        ComScope com;
        requireUserDesktop();
        auto c=mode==L"system" ? systemClient(pid) : deviceClient(eCapture,input);
        auto f=format(channels); DWORD flags=AUDCLNT_STREAMFLAGS_EVENTCALLBACK|AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM|AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY;
        if(mode==L"system") flags|=AUDCLNT_STREAMFLAGS_LOOPBACK;
        check(c->Initialize(AUDCLNT_SHAREMODE_SHARED,flags,0,0,&f,nullptr),"Cannot initialize audio capture");
        Handle event(CreateEventW(nullptr,FALSE,FALSE,nullptr)); check(c->SetEventHandle(event.value),"Capture event failed");
        ComPtr<IAudioCaptureClient> source; check(c->GetService(IID_PPV_ARGS(&source)),"Capture service unavailable");
        int err=0; std::unique_ptr<OpusEncoder,decltype(&opus_encoder_destroy)> encoder(opus_encoder_create(RATE,channels,mode==L"system"?OPUS_APPLICATION_AUDIO:OPUS_APPLICATION_VOIP,&err),opus_encoder_destroy);
        if(err!=OPUS_OK) throw std::runtime_error("Opus encoder unavailable");
        opus_encoder_ctl(encoder.get(),OPUS_SET_BITRATE(channels==2?96000:32000));
        opus_encoder_ctl(encoder.get(),OPUS_SET_COMPLEXITY(5));
        std::vector<int16_t> pending; pending.reserve(FRAME*channels*4);
        check(c->Start(),"Cannot start audio capture"); ++started;
        while(running) {
            WaitForSingleObject(event.value,50);
            if(!running) break;
            requireUserDesktop();
            UINT32 available=0; check(source->GetNextPacketSize(&available),"Capture device was lost");
            while(available && running) {
                BYTE* data=nullptr; UINT32 frames=0; DWORD status=0;
                check(source->GetBuffer(&data,&frames,&status,nullptr,nullptr),"Cannot read audio capture");
                if(status&AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY) pending.clear();
                size_t n=static_cast<size_t>(frames)*channels;
                uint32_t peak=0;
                if(mode==L"mic" && !(status&AUDCLNT_BUFFERFLAGS_SILENT)) {
                    auto samples=reinterpret_cast<int16_t*>(data);
                    for(size_t i=0;i<n;++i) peak=std::max(peak,static_cast<uint32_t>(std::abs(static_cast<int>(samples[i]))));
                    microphonePeak=peak;
                }
                if(status&AUDCLNT_BUFFERFLAGS_SILENT || muted) pending.insert(pending.end(),n,0);
                else pending.insert(pending.end(),reinterpret_cast<int16_t*>(data),reinterpret_cast<int16_t*>(data)+n);
                check(source->ReleaseBuffer(frames),"Cannot release captured audio");
                size_t offset=0;
                while(pending.size()-offset>=static_cast<size_t>(FRAME*channels)) {
                    unsigned char packet[MAX_PACKET]; int count=opus_encode(encoder.get(),pending.data()+offset,FRAME,packet,MAX_PACKET);
                    if(count<0) throw std::runtime_error("Audio encoding failed");
                    sendFrame(1,packet,count); ++capturedPackets; offset+=FRAME*channels;
                }
                pending.erase(pending.begin(),pending.begin()+offset);
                check(source->GetNextPacketSize(&available),"Capture device was lost");
            }
        }
        c->Stop();
    } catch(const std::exception& e) { failure(e); }
}
void render(int channels, const std::wstring& output) {
    try {
        ComScope com; requireUserDesktop(); auto c=deviceClient(eRender,output); auto f=format(channels);
        check(c->Initialize(AUDCLNT_SHAREMODE_SHARED,AUDCLNT_STREAMFLAGS_EVENTCALLBACK|AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM|AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,0,0,&f,nullptr),"Cannot initialize playback");
        Handle event(CreateEventW(nullptr,FALSE,FALSE,nullptr)); check(c->SetEventHandle(event.value),"Playback event failed");
        ComPtr<IAudioRenderClient> dest; check(c->GetService(IID_PPV_ARGS(&dest)),"Playback service unavailable");
        UINT32 capacity=0; check(c->GetBufferSize(&capacity),"Cannot size playback buffer");
        BYTE* initial=nullptr; check(dest->GetBuffer(capacity,&initial),"Cannot prime playback buffer");
        check(dest->ReleaseBuffer(capacity,AUDCLNT_BUFFERFLAGS_SILENT),"Cannot prime playback silence");
        check(c->Start(),"Cannot start playback"); ++started;
        while(running) {
            WaitForSingleObject(event.value,50);
            if(!running) break;
            requireUserDesktop();
            UINT32 padding=0; check(c->GetCurrentPadding(&padding),"Playback device was lost");
            UINT32 available=capacity-padding; if(!available) continue;
            BYTE* data=nullptr; check(dest->GetBuffer(available,&data),"Cannot get playback buffer");
            auto out=reinterpret_cast<int16_t*>(data); int gain=volume.load(); uint32_t consumed=0;
            {
                std::lock_guard<std::mutex> lock(pcmMutex);
                for(size_t i=0;i<static_cast<size_t>(available)*channels;++i) {
                    if(pcm.empty()) out[i]=0;
                    else { out[i]=static_cast<int16_t>(static_cast<int>(pcm.front())*gain/100); pcm.pop_front(); ++consumed; }
                }
            }
            check(dest->ReleaseBuffer(available,0),"Cannot release playback buffer");
            renderedSamples+=consumed;
        }
        c->Stop();
    } catch(const std::exception& e) { failure(e); }
}
void devices() {
    ComScope com; auto e=enumerator();
    for(auto flow : {eCapture,eRender}) {
        ComPtr<IMMDeviceCollection> collection; check(e->EnumAudioEndpoints(flow,DEVICE_STATE_ACTIVE,&collection),"Cannot list audio devices");
        UINT count=0; collection->GetCount(&count);
        for(UINT i=0;i<count;++i) {
            ComPtr<IMMDevice> d; check(collection->Item(i,&d),"Cannot inspect device"); LPWSTR id=nullptr; check(d->GetId(&id),"Cannot read device ID");
            ComPtr<IPropertyStore> properties; check(d->OpenPropertyStore(STGM_READ,&properties),"Cannot read device name"); PROPVARIANT name{};
            check(properties->GetValue(PKEY_Device_FriendlyName,&name),"Cannot read device name");
            std::cout<<(flow==eCapture?"input":"output")<<'\t'<<utf8(id)<<'\t'<<utf8(name.vt==VT_LPWSTR?name.pwszVal:L"Audio device")<<'\n';
            CoTaskMemFree(id); PropVariantClear(&name);
        }
    }
}
int selfTest() {
    int error=0;
    for(int channels : {1,2}) {
        auto enc=opus_encoder_create(RATE,channels,OPUS_APPLICATION_AUDIO,&error); if(!enc || error) return 1;
        auto dec=opus_decoder_create(RATE,channels,&error); if(!dec || error) return 2;
        std::vector<int16_t> wave(FRAME*channels,1234), decoded(FRAME*channels); unsigned char packet[MAX_PACKET];
        int size=opus_encode(enc,wave.data(),FRAME,packet,MAX_PACKET);
        int frames=opus_decode(dec,packet,size,decoded.data(),FRAME,0);
        opus_encoder_destroy(enc); opus_decoder_destroy(dec); if(size<=0 || frames!=FRAME) return 3;
    }
    std::cout<<"Opus mono/stereo self-test passed; helper protocol 1\n"; return 0;
}
int wmain(int argc, wchar_t** argv) {
    try {
        if(argc==2 && std::wstring(argv[1])==L"--devices") { devices(); return 0; }
        if(argc==2 && std::wstring(argv[1])==L"--self-test") return selfTest();
        std::wstring mode=L"none",input=L"default",output=L"default"; int channels=1; DWORD pid=0; bool play=true;
        for(int i=1;i<argc;++i) {
            std::wstring flag=argv[i]; if(i+1>=argc) throw std::runtime_error("Missing helper argument"); std::wstring value=argv[++i];
            if(flag==L"--capture") mode=value;
            else if(flag==L"--channels") channels=std::stoi(value);
            else if(flag==L"--nvda-pid") pid=std::stoul(value);
            else if(flag==L"--input") input=value;
            else if(flag==L"--output") output=value;
            else if(flag==L"--play") play=value==L"1";
            else if(flag==L"--volume") volume=std::clamp(std::stoi(value),0,100);
            else if(flag==L"--mute") muted=value==L"1";
            else throw std::runtime_error("Unknown helper argument");
        }
        if((channels!=1 && channels!=2) || (mode!=L"none" && mode!=L"mic" && mode!=L"system") || (mode==L"system" && !pid)) throw std::runtime_error("Invalid helper configuration");
        _setmode(_fileno(stdin),_O_BINARY); _setmode(_fileno(stdout),_O_BINARY);
        int err=0; std::unique_ptr<OpusDecoder,decltype(&opus_decoder_destroy)> decoder(opus_decoder_create(RATE,channels,&err),opus_decoder_destroy);
        if(err) throw std::runtime_error("Opus decoder unavailable");
        std::thread receiver, sender;
        if(play) receiver=std::thread(render,channels,output);
        if(mode!=L"none") sender=std::thread(capture,mode,channels,pid,input);
        int expected=(play?1:0)+(mode!=L"none"?1:0);
        auto deadline=std::chrono::steady_clock::now()+12s;
        while(running && started<expected && std::chrono::steady_clock::now()<deadline) std::this_thread::sleep_for(10ms);
        if(running && started==expected) sendFrame(6);
        else { running=false; std::string msg="Audio devices did not start"; sendFrame(5,msg.data(),msg.size()); }
        std::thread health([] {
            int ticks=0;
            while(running) {
                std::this_thread::sleep_for(100ms);
                if(running && ++ticks==10) {
                    ticks=0;
                    uint32_t stats[]={capturedPackets.load(),decodedPackets.load(),renderedSamples.load(),microphonePeak.load()};
                    sendFrame(7,stats,sizeof(stats));
                }
            }
        });
        // On worker failure parent receives type 5 and closes stdin, unblocking this read.
        while(running) {
            uint32_t size=0; if(!readAll(&size,4)) break;
            if(size<1 || size>MAX_PACKET+1) break;
            std::vector<uint8_t> data(size); if(!readAll(data.data(),size)) break;
            if(data[0]==4) break;
            if(data[0]==2 && size==2) { muted=data[1]!=0; continue; }
            if(data[0]==3 && size==2) { volume=std::min<int>(data[1],100); continue; }
            if(data[0]!=1 || !play || size<2) continue;
            if(opus_packet_get_nb_samples(data.data()+1,size-1,RATE)!=FRAME) continue;
            std::vector<int16_t> samples(FRAME*channels);
            int frames=opus_decode(decoder.get(),data.data()+1,size-1,samples.data(),FRAME,0);
            if(frames!=FRAME) continue;
            ++decodedPackets;
            std::lock_guard<std::mutex> lock(pcmMutex);
            constexpr int BUFFER_FRAMES=6;
            while(pcm.size()+samples.size()>static_cast<size_t>(BUFFER_FRAMES*FRAME*channels)) {
                size_t discard=std::min<size_t>(FRAME*channels,pcm.size()); if(!discard) break;
                pcm.erase(pcm.begin(),pcm.begin()+discard);
            }
            pcm.insert(pcm.end(),samples.begin(),samples.end());
        }
        running=false; if(sender.joinable()) sender.join(); if(receiver.joinable()) receiver.join(); health.join(); return started==expected?0:1;
    } catch(const std::exception& e) { std::cerr<<e.what()<<'\n'; return 1; }
}
