// audiorec — record Teams (or system) audio and the microphone into two 16 kHz mono WAV files.
//
//   audiorec start --out DIR [--all-audio] [--force-sck] [--mic-only] [--max-seconds N]
//                                                               record until SIGINT
//   audiorec check [--request]                                  permission status as JSON
//   audiorec list                                               processes known to Core Audio
//
// "Others" comes from a Core Audio process tap on Teams (macOS 14.2+; global tap if Teams isn't
// running), falling back to ScreenCaptureKit. "Me" is the default input device via AVAudioEngine.
// --mic-only (voice notes) records just me.wav. Both files share one start time: each channel is padded with silence to stay on the host clock.
// WAV headers are rewritten every few seconds, so a crash leaves usable files.

import AVFoundation
import AudioToolbox
import CoreAudio
import CoreGraphics
import CoreMedia
import Foundation
import ScreenCaptureKit

// MARK: - Helpers

func emit(_ dict: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: dict, options: [.sortedKeys]),
       let line = String(data: data, encoding: .utf8) {
        print(line)
        fflush(stdout)
    }
}

func warn(_ message: String) {
    FileHandle.standardError.write(Data((message + "\n").utf8))
}

struct AudioError: Error, CustomStringConvertible {
    let description: String
}

func check(_ status: OSStatus, _ what: String) throws {
    if status != noErr { throw AudioError(description: "\(what) failed (OSStatus \(status))") }
}

let tickSeconds: Double = {
    var info = mach_timebase_info_data_t()
    mach_timebase_info(&info)
    return Double(info.numer) / Double(info.denom) / 1e9
}()

func localISO(_ date: Date) -> String {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
    return f.string(from: date)
}

func copyBuffer(_ src: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
    guard let dst = AVAudioPCMBuffer(pcmFormat: src.format, frameCapacity: src.frameLength) else { return nil }
    dst.frameLength = src.frameLength
    let s = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: src.audioBufferList))
    let d = UnsafeMutableAudioBufferListPointer(dst.mutableAudioBufferList)
    for i in 0..<min(s.count, d.count) {
        guard let from = s[i].mData, let to = d[i].mData else { continue }
        let bytes = min(s[i].mDataByteSize, d[i].mDataByteSize)
        memcpy(to, from, Int(bytes))
        d[i].mDataByteSize = bytes
    }
    return dst
}

// MARK: - WAV writer (16 kHz mono 16-bit, header refreshed every 5 s)

final class WavWriter {
    private let handle: FileHandle
    private(set) var frames: Int64 = 0
    private var lastFlush = Date()

    init(url: URL) throws {
        FileManager.default.createFile(atPath: url.path, contents: nil)
        handle = try FileHandle(forWritingTo: url)
        handle.write(Self.header(dataBytes: 0))
    }

    static func header(dataBytes: UInt32) -> Data {
        var d = Data()
        func u32(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
        func u16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
        d.append(Data("RIFF".utf8)); u32(36 + dataBytes)
        d.append(Data("WAVEfmt ".utf8)); u32(16); u16(1); u16(1); u32(16000); u32(32000); u16(2); u16(16)
        d.append(Data("data".utf8)); u32(dataBytes)
        return d
    }

    func append(_ samples: UnsafeBufferPointer<Int16>) {
        handle.write(Data(buffer: samples))
        frames += Int64(samples.count)
        if Date().timeIntervalSince(lastFlush) > 5 { flush() }
    }

    func appendSilence(_ count: Int) {
        guard count > 0 else { return }
        handle.write(Data(count: count * 2))
        frames += Int64(count)
    }

    func flush() {
        let end = handle.offsetInFile
        handle.seek(toFileOffset: 0)
        handle.write(Self.header(dataBytes: UInt32(clamping: frames * 2)))
        handle.seek(toFileOffset: end)
        handle.synchronizeFile()
        lastFlush = Date()
    }

    func close() {
        flush()
        handle.closeFile()
    }
}

// MARK: - Channel: converts any input format to 16 kHz mono Int16 and keeps it on the clock

final class Channel {
    let name: String
    private let writer: WavWriter
    private let t0: UInt64
    private let outFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
    private var converter: AVAudioConverter?
    private var closed = false
    private(set) var peak: Int16 = 0

    init(name: String, url: URL, t0: UInt64) throws {
        self.name = name
        self.t0 = t0
        writer = try WavWriter(url: url)
    }

    var seconds: Double { Double(writer.frames) / 16000 }

    /// Must be called on the writer queue.
    func write(_ buffer: AVAudioPCMBuffer, hostTime: UInt64) {
        guard !closed, buffer.frameLength > 0 else { return }
        // Pad with silence if this channel is behind the shared clock (late start, dropouts).
        let elapsed = hostTime > t0 ? Double(hostTime - t0) * tickSeconds : 0
        let behind = Int64(elapsed * 16000) - writer.frames
        if behind > 4000 { writer.appendSilence(Int(behind)) }

        if converter == nil || converter!.inputFormat != buffer.format {
            converter = AVAudioConverter(from: buffer.format, to: outFormat)
            converter?.downmix = true
        }
        guard let converter else { return }
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * 16000 / buffer.format.sampleRate) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: capacity) else { return }
        var fed = false
        var error: NSError?
        converter.convert(to: out, error: &error) { _, status in
            if fed {
                status.pointee = .noDataNow
                return nil
            }
            fed = true
            status.pointee = .haveData
            return buffer
        }
        if let error { warn("\(name): conversion error: \(error)") }
        if let p = out.int16ChannelData?[0], out.frameLength > 0 {
            let samples = UnsafeBufferPointer(start: p, count: Int(out.frameLength))
            for s in samples where s != Int16.min && abs(s) > peak { peak = abs(s) }
            writer.append(samples)
        }
    }

    func close() {
        guard !closed else { return }
        closed = true
        writer.close()
    }
}

// MARK: - Core Audio properties

let systemObject = AudioObjectID(kAudioObjectSystemObject)

func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                               mElement: kAudioObjectPropertyElementMain)
}

func getValue<T>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, _ initial: T) -> T? {
    var addr = address(selector)
    var value = initial
    var size = UInt32(MemoryLayout<T>.size)
    let status = withUnsafeMutablePointer(to: &value) { AudioObjectGetPropertyData(object, &addr, 0, nil, &size, $0) }
    return status == noErr ? value : nil
}

func getString(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    getValue(object, selector, "" as CFString).map { $0 as String }
}

func getObjectList(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> [AudioObjectID] {
    var addr = address(selector)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(object, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &ids) == noErr else { return [] }
    return ids
}

struct AudioProcess {
    let id: AudioObjectID
    let pid: pid_t
    let bundleID: String
}

func audioProcesses() -> [AudioProcess] {
    guard #available(macOS 14.2, *) else { return [] }
    return getObjectList(systemObject, kAudioHardwarePropertyProcessObjectList).map { id in
        AudioProcess(id: id, pid: getValue(id, kAudioProcessPropertyPID, pid_t(0)) ?? 0,
                     bundleID: getString(id, kAudioProcessPropertyBundleID) ?? "")
    }
}

func teamsProcesses() -> [AudioProcess] {
    let all = audioProcesses()
    let teams2 = all.filter { $0.bundleID.hasPrefix("com.microsoft.teams2") }
    return teams2.isEmpty ? all.filter { $0.bundleID.hasPrefix("com.microsoft.teams") } : teams2
}

// MARK: - System audio via a Core Audio process tap (macOS 14.2+)

@available(macOS 14.2, *)
final class TapCapture {
    private let channel: Channel
    private let queue: DispatchQueue
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID?
    private(set) var label = ""
    private var tappedIDs: Set<AudioObjectID> = []
    private let allAudio: Bool

    init(channel: Channel, queue: DispatchQueue, allAudio: Bool) {
        self.channel = channel
        self.queue = queue
        self.allAudio = allAudio
    }

    func start() throws {
        let teams = allAudio ? [] : teamsProcesses()
        let description: CATapDescription
        if teams.isEmpty {
            let me = audioProcesses().filter { $0.pid == getpid() }.map(\.id)
            description = CATapDescription(stereoGlobalTapButExcludeProcesses: me)
            label = "tap:global"
        } else {
            description = CATapDescription(stereoMixdownOfProcesses: teams.map(\.id))
            label = "tap:teams"
        }
        tappedIDs = Set(teams.map(\.id))
        description.uuid = UUID()
        description.isPrivate = true
        description.muteBehavior = .unmuted
        try check(AudioHardwareCreateProcessTap(description, &tapID), "AudioHardwareCreateProcessTap")

        guard var asbd = getValue(tapID, kAudioTapPropertyFormat, AudioStreamBasicDescription()),
              let format = AVAudioFormat(streamDescription: &asbd) else {
            throw AudioError(description: "could not read the tap format")
        }
        guard let outputDevice = getValue(systemObject, kAudioHardwarePropertyDefaultOutputDevice, AudioDeviceID(0)),
              let outputUID = getString(outputDevice, kAudioDevicePropertyDeviceUID) else {
            throw AudioError(description: "no default output device")
        }
        let aggregate: [String: Any] = [
            kAudioAggregateDeviceNameKey: "audiorec-tap",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceMainSubDeviceKey: outputUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true,
                                               kAudioSubTapUIDKey: description.uuid.uuidString]],
        ]
        try check(AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &aggregateID),
                  "AudioHardwareCreateAggregateDevice")
        let channel = self.channel, queue = self.queue
        try check(AudioDeviceCreateIOProcIDWithBlock(&procID, aggregateID, nil) { _, inData, inTime, _, _ in
            guard let view = AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: inData, deallocator: nil),
                  let copy = copyBuffer(view) else { return }
            let host = inTime.pointee.mHostTime
            queue.async { channel.write(copy, hostTime: host) }
        }, "AudioDeviceCreateIOProcIDWithBlock")
        try check(AudioDeviceStart(aggregateID, procID), "AudioDeviceStart")
    }

    func stop() {
        if aggregateID != kAudioObjectUnknown {
            if let procID {
                AudioDeviceStop(aggregateID, procID)
                AudioDeviceDestroyIOProcID(aggregateID, procID)
            }
            AudioHardwareDestroyAggregateDevice(aggregateID)
        }
        if tapID != kAudioObjectUnknown { AudioHardwareDestroyProcessTap(tapID) }
        procID = nil
        aggregateID = AudioObjectID(kAudioObjectUnknown)
        tapID = AudioObjectID(kAudioObjectUnknown)
    }

    /// Rebuild the tap when the output device changes or Teams' audio processes change.
    func watchForChanges() {
        let restart: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            guard let self else { return }
            let teamsNow = Set(self.allAudio ? [] : teamsProcesses().map(\.id))
            if teamsNow == self.tappedIDs && self.aggregateID != kAudioObjectUnknown { return }
            self.restart(reason: "Teams audio processes changed")
        }
        var procs = address(kAudioHardwarePropertyProcessObjectList)
        AudioObjectAddPropertyListenerBlock(systemObject, &procs, queue, restart)
        var output = address(kAudioHardwarePropertyDefaultOutputDevice)
        AudioObjectAddPropertyListenerBlock(systemObject, &output, queue) { [weak self] _, _ in
            self?.restart(reason: "default output device changed")
        }
    }

    private func restart(reason: String) {
        stop()
        do {
            try start()
            emit(["event": "others_restarted", "reason": reason, "others": label])
        } catch {
            warn("tap restart failed: \(error)")
        }
    }
}

// MARK: - System audio via ScreenCaptureKit (fallback)

final class SCKCapture: NSObject, SCStreamOutput, SCStreamDelegate {
    private let channel: Channel
    private let queue: DispatchQueue
    private var stream: SCStream?
    private let discard = DispatchQueue(label: "audiorec.discard")

    init(channel: Channel, queue: DispatchQueue) {
        self.channel = channel
        self.queue = queue
    }

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else { throw AudioError(description: "no display") }
        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = 48000
        config.channelCount = 2
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: discard)
        try await stream.startCapture()
        self.stream = stream
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sample: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sample.isValid, let desc = sample.formatDescription else { return }
        let format = AVAudioFormat(cmAudioFormatDescription: desc)
        let frames = AVAudioFrameCount(sample.numSamples)
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames) else { return }
        buffer.frameLength = frames
        let status = CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sample, at: 0, frameCount: Int32(frames), into: buffer.mutableAudioBufferList)
        guard status == noErr else { return }
        let host = CMClockConvertHostTimeToSystemUnits(sample.presentationTimeStamp)
        channel.write(buffer, hostTime: host)  // already on the writer queue
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        warn("ScreenCaptureKit stopped: \(error)")
        emit(["event": "error", "channel": "others", "message": "\(error)"])
    }

    func stop() {
        let done = DispatchSemaphore(value: 0)
        stream?.stopCapture { _ in done.signal() }
        _ = done.wait(timeout: .now() + 2)
    }
}

// MARK: - Microphone via AVAudioEngine

final class MicCapture {
    private let engine = AVAudioEngine()
    private let channel: Channel
    private let queue: DispatchQueue
    private var observer: NSObjectProtocol?

    init(channel: Channel, queue: DispatchQueue) {
        self.channel = channel
        self.queue = queue
    }

    func start() throws {
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw AudioError(description: "no input device")
        }
        let channel = self.channel, queue = self.queue
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 4096, format: format) { buffer, time in
            guard let copy = copyBuffer(buffer) else { return }
            let host = time.isHostTimeValid ? time.hostTime : mach_absolute_time()
            queue.async { channel.write(copy, hostTime: host) }
        }
        engine.prepare()
        try engine.start()
        if observer == nil {
            // Input device changed (headset plugged in, AirPods connected): restart on the new one.
            observer = NotificationCenter.default.addObserver(
                forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil) { [weak self] _ in
                self?.queue.async {
                    guard let self else { return }
                    self.engine.stop()
                    do {
                        try self.start()
                        emit(["event": "me_restarted"])
                    } catch {
                        warn("mic restart failed: \(error)")
                    }
                }
            }
        }
    }

    func stop() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
    }
}

// MARK: - Permissions (TCC)

private let tcc = dlopen("/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC", RTLD_NOW)

func systemAudioPermission() -> String {
    typealias Preflight = @convention(c) (CFString, CFDictionary?) -> Int
    guard let tcc, let sym = dlsym(tcc, "TCCAccessPreflight") else { return "unknown" }
    switch unsafeBitCast(sym, to: Preflight.self)("kTCCServiceAudioCapture" as CFString, nil) {
    case 0: return "granted"
    case 1: return "denied"
    default: return "undetermined"
    }
}

func requestSystemAudioPermission() -> Bool {
    typealias Request = @convention(c) (CFString, CFDictionary?, @escaping @convention(block) (Bool) -> Void) -> Void
    guard let tcc, let sym = dlsym(tcc, "TCCAccessRequest") else { return false }
    let done = DispatchSemaphore(value: 0)
    var granted = false
    unsafeBitCast(sym, to: Request.self)("kTCCServiceAudioCapture" as CFString, nil) { ok in
        granted = ok
        done.signal()
    }
    _ = done.wait(timeout: .now() + 120)
    return granted
}

func microphonePermission() -> String {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: return "granted"
    case .denied, .restricted: return "denied"
    default: return "undetermined"
    }
}

func requestMicrophonePermission() -> Bool {
    let done = DispatchSemaphore(value: 0)
    var granted = false
    AVCaptureDevice.requestAccess(for: .audio) { ok in
        granted = ok
        done.signal()
    }
    _ = done.wait(timeout: .now() + 120)
    return granted
}

func permissions() -> [String: Any] {
    var tapsSupported = false
    if #available(macOS 14.2, *) { tapsSupported = true }
    return ["microphone": microphonePermission(), "system_audio": systemAudioPermission(),
            "screen_recording": CGPreflightScreenCaptureAccess() ? "granted" : "not granted",
            "process_taps_supported": tapsSupported]
}

// MARK: - Commands

func runCheck(request: Bool) -> Never {
    if request {
        if microphonePermission() == "undetermined" { _ = requestMicrophonePermission() }
        if systemAudioPermission() == "undetermined" { _ = requestSystemAudioPermission() }
    }
    emit(permissions())
    exit(0)
}

func runList() -> Never {
    for p in audioProcesses() {
        emit(["id": p.id, "pid": p.pid, "bundle_id": p.bundleID])
    }
    exit(0)
}

final class Recorder {
    let folder: URL
    let queue = DispatchQueue(label: "audiorec.writer")
    var others: Channel!
    var me: Channel!
    var tap: AnyObject?
    var sck: SCKCapture?
    var mic: MicCapture?
    var stopping = false
    let startedAt = Date()
    var session: [String: Any] = [:]

    init(folder: URL) {
        self.folder = folder
    }

    func start(forceSCK: Bool, allAudio: Bool, micOnly: Bool) async {
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            let t0 = mach_absolute_time()
            if !micOnly {
                others = try Channel(name: "others", url: folder.appendingPathComponent("others.wav"), t0: t0)
            }
            me = try Channel(name: "me", url: folder.appendingPathComponent("me.wav"), t0: t0)
        } catch {
            emit(["event": "error", "message": "cannot create output files: \(error)"])
            exit(1)
        }
        session = ["started": localISO(startedAt), "pid": getpid(), "mode": micOnly ? "voice-note" : "meeting"]

        // Microphone
        var meStatus = microphonePermission()
        if meStatus == "undetermined" { meStatus = requestMicrophonePermission() ? "granted" : "denied" }
        if meStatus == "granted" {
            let mic = MicCapture(channel: me, queue: queue)
            do {
                try mic.start()
                self.mic = mic
                meStatus = "recording"
            } catch {
                meStatus = "error: \(error)"
            }
        }

        // Others: process tap first, ScreenCaptureKit as fallback.
        var othersStatus = micOnly ? "off" : "none"
        var tapError = ""
        if #available(macOS 14.2, *), !forceSCK, !micOnly {
            let tap = TapCapture(channel: others, queue: queue, allAudio: allAudio)
            do {
                try tap.start()
                tap.watchForChanges()
                self.tap = tap
                othersStatus = tap.label
            } catch {
                tapError = "\(error)"
                tap.stop()
            }
        }
        if tap == nil && !micOnly {
            let sck = SCKCapture(channel: others, queue: queue)
            do {
                try await sck.start()
                self.sck = sck
                othersStatus = "sck"
            } catch {
                othersStatus = "error: \(error)" + (tapError.isEmpty ? "" : " (tap: \(tapError))")
            }
        }
        if mic == nil && tap == nil && sck == nil {
            emit(["event": "error", "message": "nothing to record", "me": meStatus, "others": othersStatus])
            finish(code: 3)
        }

        session["others"] = othersStatus
        session["me"] = meStatus
        writeSession()
        var started: [String: Any] = ["event": "started", "folder": folder.path, "me": meStatus, "others": othersStatus,
                                      "system_audio_permission": systemAudioPermission()]
        if !tapError.isEmpty { started["tap_error"] = tapError }
        emit(started)
    }

    func writeSession() {
        if let data = try? JSONSerialization.data(withJSONObject: session, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: folder.appendingPathComponent("session.json"), options: .atomic)
        }
    }

    func finish(code: Int32 = 0) -> Never {
        mic?.stop()
        if #available(macOS 14.2, *) { (tap as? TapCapture)?.stop() }
        sck?.stop()
        queue.sync {
            others?.close()
            me?.close()
        }
        session["stopped"] = localISO(Date())
        session["others_seconds"] = others.map { ($0.seconds * 10).rounded() / 10 }
        session["me_seconds"] = me.map { ($0.seconds * 10).rounded() / 10 }
        session["others_peak"] = others.map { Int($0.peak) }
        session["me_peak"] = me.map { Int($0.peak) }
        writeSession()
        var stopped = session
        stopped["event"] = "stopped"
        emit(stopped)
        exit(code)
    }
}

func runStart(args: [String]) -> Never {
    guard let i = args.firstIndex(of: "--out"), i + 1 < args.count else {
        warn("usage: audiorec start --out DIR [--all-audio] [--force-sck] [--mic-only] [--max-seconds N]")
        exit(2)
    }
    let recorder = Recorder(folder: URL(fileURLWithPath: args[i + 1]))
    let maxSeconds = args.firstIndex(of: "--max-seconds").flatMap { Double(args[$0 + 1]) } ?? 4 * 3600
    let parent = getppid()

    var sources: [DispatchSourceSignal] = []
    for sig in [SIGINT, SIGTERM, SIGHUP] {
        signal(sig, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        source.setEventHandler { recorder.finish() }
        source.resume()
        sources.append(source)
    }
    // Stop when the parent dies (menu-bar app crashed) or after the maximum duration.
    let watchdog = DispatchSource.makeTimerSource(queue: .main)
    watchdog.schedule(deadline: .now() + 1, repeating: 1)
    watchdog.setEventHandler {
        if getppid() != parent { recorder.finish() }
        if Date().timeIntervalSince(recorder.startedAt) > maxSeconds { recorder.finish() }
    }
    watchdog.resume()

    Task { await recorder.start(forceSCK: args.contains("--force-sck"), allAudio: args.contains("--all-audio"),
                                micOnly: args.contains("--mic-only")) }
    withExtendedLifetime((sources, watchdog)) { dispatchMain() }
}

// MARK: - Responsibility: run as our own TCC identity

// macOS attributes privacy permissions to the "responsible" process — normally whatever launched
// us (Terminal, the uv Python of the menu-bar app). Re-spawning ourselves with responsibility
// disclaimed makes AudioRec.app itself the one that asks for and holds the permissions.
var child: pid_t = 0

func spawnDisclaimed() -> Never {
    typealias Disclaim = @convention(c) (UnsafeMutablePointer<posix_spawnattr_t?>, Int32) -> Int32
    var attr: posix_spawnattr_t?
    posix_spawnattr_init(&attr)
    if let sym = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "responsibility_spawnattrs_setdisclaim") {
        _ = unsafeBitCast(sym, to: Disclaim.self)(&attr, 1)
    }
    setenv("AUDIOREC_CHILD", "1", 1)
    let path = Bundle.main.executablePath ?? CommandLine.arguments[0]
    var argv: [UnsafeMutablePointer<CChar>?] = CommandLine.arguments.map { strdup($0) } + [nil]
    let status = posix_spawn(&child, path, nil, &attr, &argv, environ)
    guard status == 0 else {
        warn("posix_spawn failed: \(status)")
        exit(1)
    }
    for sig in [SIGINT, SIGTERM, SIGHUP] {
        signal(sig) { s in kill(child, s) }
    }
    let parent = getppid()
    Thread.detachNewThread {
        while true {
            sleep(1)
            if getppid() != parent {  // our launcher died: stop the recording cleanly
                kill(child, SIGINT)
                return
            }
        }
    }
    var result: Int32 = 0
    while waitpid(child, &result, 0) == -1 && errno == EINTR {}
    exit((result & 0x7f) == 0 ? (result >> 8) & 0xff : 128 + (result & 0x7f))
}

// MARK: - Main

let args = Array(CommandLine.arguments.dropFirst())
if getenv("AUDIOREC_CHILD") == nil && ["start", "check"].contains(args.first ?? "") {
    spawnDisclaimed()
}
switch args.first {
case "start": runStart(args: args)
case "check": runCheck(request: args.contains("--request"))
case "list": runList()
default:
    warn("usage: audiorec start --out DIR [--all-audio] [--force-sck] [--mic-only] [--max-seconds N] | check [--request] | list")
    exit(2)
}
