import AppKit
import Foundation

private struct RecordingPreset: Sendable {
    let language: String
    let model: String
}

/// Asked before every recording, in the language the meeting is recorded
/// in so the notice can be pasted into the meeting chat as is. Auto detect
/// has no language yet, so it gets English.
private struct ConsentPrompt {
    let title: String
    let message: String
    let record: String
    let copyAndRecord: String
    let cancel: String
    let dontAsk: String
    let notice: String

    static func forLanguage(_ language: String) -> ConsentPrompt {
        switch language {
        case "it":
            return ConsentPrompt(
                title: "Hai informato i partecipanti?",
                message: "Registra solo se tutti i partecipanti sanno che "
                    + "stai registrando. In alcuni paesi, tra cui la "
                    + "Germania e diversi stati USA, serve il consenso di "
                    + "tutti.",
                record: "Sì, registra",
                copyAndRecord: "Copia avviso e registra",
                cancel: "Annulla",
                dontAsk: "Non chiedere più",
                notice: "Registro questa riunione sul mio Mac per prendere "
                    + "appunti; l'audio resta sul mio computer e non viene "
                    + "caricato da nessuna parte. Ditemi se preferite di no."
            )
        case "es":
            return ConsentPrompt(
                title: "¿Has informado a los participantes?",
                message: "Graba solo si todos los participantes saben que "
                    + "estás grabando. En algunos lugares, como Alemania y "
                    + "varios estados de EE. UU., se necesita el "
                    + "consentimiento de todos.",
                record: "Sí, grabar",
                copyAndRecord: "Copiar aviso y grabar",
                cancel: "Cancelar",
                dontAsk: "No volver a preguntar",
                notice: "Estoy grabando esta reunión en mi Mac para tomar "
                    + "notas; el audio se queda en mi ordenador y no se sube "
                    + "a ningún sitio. Decidme si preferís que no lo haga."
            )
        default:
            return ConsentPrompt(
                title: "Have you told the participants?",
                message: "Record only if everyone in the meeting knows you "
                    + "are recording. In some places, including Germany and "
                    + "several US states, everyone has to consent.",
                record: "Yes, record",
                copyAndRecord: "Copy notice and record",
                cancel: "Cancel",
                dontAsk: "Don't ask again",
                notice: "I'm recording this meeting on my Mac to take notes; "
                    + "the audio stays on my computer and is not uploaded "
                    + "anywhere. Let me know if you'd rather I didn't."
            )
        }
    }
}

private struct JobProgress: Decodable {
    let phase: String
    let fraction: Double
    let detail: String
    let indeterminate: Bool
}

private struct JobStatus: Decodable {
    let state: String
    let progress: JobProgress?
}

private final class SessionLog: @unchecked Sendable {
    private let handle: FileHandle
    private let lock = NSLock()

    init(url: URL) throws {
        FileManager.default.createFile(atPath: url.path, contents: nil)
        guard let handle = FileHandle(forWritingAtPath: url.path) else {
            throw CocoaError(.fileWriteUnknown)
        }
        self.handle = handle
    }

    func write(_ message: String) {
        lock.lock()
        defer { lock.unlock() }
        try? handle.write(contentsOf: Data("\(message)\n".utf8))
    }

    func close() {
        lock.lock()
        defer { lock.unlock() }
        try? handle.close()
    }
}

final class MenuController: NSObject, @unchecked Sendable {
    private let statusItem = NSStatusBar.system.statusItem(
        withLength: NSStatusItem.variableLength
    )
    /// Looked up in the app bundle's Resources, where `meetrec.sh` copies
    /// them (the `@2x` file is picked up automatically). Nil when the binary
    /// runs outside the bundle; `setStatus` then falls back to text.
    private static let idleImage = MenuController.templateImage(
        named: "MenuBarIdleTemplate",
        description: "MeetRec"
    )
    private static let recordingImage = MenuController.templateImage(
        named: "MenuBarRecTemplate",
        description: "MeetRec — recording"
    )
    private let projectURL: URL
    private let recordingsURL: URL
    private let pythonURL: URL
    /// Recovery at launch and post-recording transcription both run
    /// `worker --once`. A serial queue keeps them from overlapping: the
    /// second run starts after the first finishes and picks up whatever it
    /// enqueued in the meantime.
    private let workerQueue = DispatchQueue(
        label: "meetrec.transcription-worker",
        qos: .utility
    )

    private var activeRecorder: Recorder?
    private var activeLog: SessionLog?
    private var activeSessionURL: URL?
    private var activePreset: RecordingPreset?
    private var startedAt: Date?
    private var timer: Timer?
    private var processingSessionURL: URL?
    private var processingTimer: Timer?
    private var currentProgress: JobProgress?
    private var progressLabel: NSTextField?
    private var progressIndicator: NSProgressIndicator?
    /// Lives in the Python config so the worker, which does the deleting,
    /// reads the same value. Nil until the first read comes back.
    private var deleteAudioEnabled: Bool?
    private static let skipConsentPromptKey = "skipConsentPrompt"

    init(projectURL: URL) {
        self.projectURL = projectURL
        let pythonURL = projectURL.appendingPathComponent(".venv/bin/python")
        self.pythonURL = pythonURL
        self.recordingsURL = MenuController.resolveRecordingsURL(
            projectURL: projectURL,
            pythonURL: pythonURL
        )
        super.init()
        configureMenu()
        loadDeleteAudioSetting()
        resumePendingTranscriptions()
    }

    /// The worker scans the directory the Python config names, so recording
    /// anywhere else leaves sessions it never finds. Ask Python rather than
    /// re-implement the precedence here; fall back to the old guess only when
    /// it cannot answer.
    private static func resolveRecordingsURL(
        projectURL: URL,
        pythonURL: URL
    ) -> URL {
        if let text = controlOutput(
            ["recordings-dir"],
            projectURL: projectURL,
            pythonURL: pythonURL
        ) {
            return URL(fileURLWithPath: text, isDirectory: true)
        }
        return ProcessInfo.processInfo.environment["RECORDINGS_DIR"]
            .map { URL(fileURLWithPath: $0, isDirectory: true) }
            ?? projectURL.appendingPathComponent("recordings", isDirectory: true)
    }

    /// Runs a control command and returns its trimmed output, or nil when it
    /// fails or prints nothing.
    private static func controlOutput(
        _ arguments: [String],
        projectURL: URL,
        pythonURL: URL
    ) -> String? {
        let process = Process()
        let output = Pipe()
        process.executableURL = pythonURL
        process.arguments = ["-m", "meeting_recorder.control"] + arguments
        process.currentDirectoryURL = projectURL
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            let data = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            if process.terminationStatus == 0,
               let text = String(data: data, encoding: .utf8)?
                   .trimmingCharacters(in: .whitespacesAndNewlines),
               !text.isEmpty
            {
                return text
            }
        } catch {}
        return nil
    }

    private static func templateImage(
        named name: String,
        description: String
    ) -> NSImage? {
        let image = NSImage(named: name)
        image?.isTemplate = true
        image?.accessibilityDescription = description
        return image
    }

    /// Shows the idle or recording icon with `text` beside it. Without the
    /// bundled image, falls back to the old text glyphs.
    private func setStatus(recording: Bool, text: String) {
        guard let button = statusItem.button else { return }
        let image = recording
            ? MenuController.recordingImage
            : MenuController.idleImage
        if let image {
            button.image = image
            button.imagePosition = .imageLeading
            button.title = text.isEmpty ? "" : " \(text)"
        } else {
            button.image = nil
            button.imagePosition = .noImage
            let glyph = recording ? "●" : "✒︎"
            button.title = text.isEmpty ? glyph : "\(glyph) \(text)"
        }
    }

    private func configureMenu() {
        setStatus(recording: false, text: "")
        rebuildMenu()
    }

    private func rebuildMenu() {
        let menu = NSMenu()
        menu.addItem(
            withTitle: "About MeetRec",
            action: #selector(showAbout),
            keyEquivalent: ""
        ).target = self
        menu.addItem(.separator())
        if processingSessionURL != nil {
            addProgressItem(to: menu)
            menu.addItem(.separator())
        }
        if activeRecorder == nil {
            menu.addItem(
                withTitle: "Start recording — Italiano",
                action: #selector(startItalian),
                keyEquivalent: "i"
            ).target = self
            menu.addItem(
                withTitle: "Start recording — English",
                action: #selector(startEnglish),
                keyEquivalent: "e"
            ).target = self
            menu.addItem(
                withTitle: "Start recording — Español",
                action: #selector(startSpanish),
                keyEquivalent: "s"
            ).target = self
            menu.addItem(
                withTitle: "Start recording — Auto detect",
                action: #selector(startAuto),
                keyEquivalent: "a"
            ).target = self
        } else {
            menu.addItem(
                withTitle: "Stop recording",
                action: #selector(stopRecording),
                keyEquivalent: "r"
            ).target = self
        }
        menu.addItem(.separator())
        menu.addItem(
            withTitle: "Open recordings",
            action: #selector(openRecordings),
            keyEquivalent: "o"
        ).target = self
        menu.addItem(
            withTitle: "Run doctor",
            action: #selector(runDoctor),
            keyEquivalent: "d"
        ).target = self
        menu.addItem(.separator())
        let consentItem = menu.addItem(
            withTitle: "Ask about participants before recording",
            action: #selector(toggleConsentPrompt),
            keyEquivalent: ""
        )
        consentItem.target = self
        consentItem.state = UserDefaults.standard.bool(
            forKey: MenuController.skipConsentPromptKey
        ) ? .off : .on
        let deleteAudioItem = menu.addItem(
            withTitle: "Delete audio after a clean transcript",
            action: deleteAudioEnabled == nil
                ? nil
                : #selector(toggleDeleteAudio),
            keyEquivalent: ""
        )
        deleteAudioItem.target = self
        deleteAudioItem.state = deleteAudioEnabled == true ? .on : .off
        deleteAudioItem.toolTip = "The audio is kept when the transcript "
            + "failed, is empty or has warnings, so it can be transcribed "
            + "again."
        menu.addItem(.separator())
        menu.addItem(
            withTitle: "Quit MeetRec",
            action: #selector(quit),
            keyEquivalent: "q"
        ).target = self
        statusItem.menu = menu
    }

    private func addProgressItem(to menu: NSMenu) {
        let item = NSMenuItem()
        let view = NSView(frame: NSRect(x: 0, y: 0, width: 320, height: 58))
        let label = NSTextField(labelWithString: "Preparing transcript…")
        label.frame = NSRect(x: 14, y: 32, width: 292, height: 18)
        label.font = NSFont.systemFont(ofSize: 12, weight: .medium)
        label.lineBreakMode = .byTruncatingTail

        let indicator = NSProgressIndicator(
            frame: NSRect(x: 14, y: 12, width: 292, height: 12)
        )
        indicator.style = .bar
        indicator.controlSize = .small
        indicator.minValue = 0
        indicator.maxValue = 1

        view.addSubview(label)
        view.addSubview(indicator)
        item.view = view
        menu.addItem(item)
        progressLabel = label
        progressIndicator = indicator
        updateProgressUI()
    }

    @objc private func startItalian() {
        startRecording(
            preset: RecordingPreset(language: "it", model: "medium")
        )
    }

    @objc private func startEnglish() {
        // Deliberately the multilingual model, matching every other preset.
        // Whisper's .en variants gain a fraction of a WER point on pure
        // English and cannot transcribe another language at all, so picking
        // one here turned "this meeting is in English" into "nothing said in
        // Italian will survive" - which is exactly what happened to a call
        // that switched language halfway through.
        startRecording(
            preset: RecordingPreset(language: "en", model: "medium")
        )
    }

    @objc private func startSpanish() {
        startRecording(
            preset: RecordingPreset(language: "es", model: "medium")
        )
    }

    @objc private func startAuto() {
        startRecording(
            preset: RecordingPreset(language: "auto", model: "medium")
        )
    }

    private func startRecording(preset: RecordingPreset) {
        guard activeRecorder == nil else { return }
        guard FileManager.default.isExecutableFile(atPath: pythonURL.path) else {
            showAlert(
                title: "Python environment missing",
                message: "Create .venv and install the project requirements."
            )
            return
        }
        guard confirmParticipantsInformed(language: preset.language) else {
            return
        }

        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd'T'HH-mm-ss"
        let sessionURL = recordingsURL.appendingPathComponent(
            formatter.string(from: Date()),
            isDirectory: true
        )
        do {
            try FileManager.default.createDirectory(
                at: sessionURL,
                withIntermediateDirectories: true
            )
            let logURL = sessionURL.appendingPathComponent("recorder.log")
            let sessionLog = try SessionLog(url: logURL)
            let recorder = Recorder(
                sessionURL: sessionURL,
                showVUMeter: false
            ) { message in
                sessionLog.write(message)
            }

            activeRecorder = recorder
            activeLog = sessionLog
            activeSessionURL = sessionURL
            activePreset = preset
            startedAt = Date()
            startTimer()
            rebuildMenu()

            Task { @MainActor [weak self, recorder] in
                do {
                    try await recorder.start()
                } catch {
                    recorder.markFailed(error)
                    self?.recordingFinished(
                        sessionURL: sessionURL,
                        status: 1
                    )
                }
            }
        } catch {
            showAlert(
                title: "Could not start recording",
                message: error.localizedDescription
            )
        }
    }

    private func confirmParticipantsInformed(language: String) -> Bool {
        let defaults = UserDefaults.standard
        if defaults.bool(forKey: MenuController.skipConsentPromptKey) {
            return true
        }
        let prompt = ConsentPrompt.forLanguage(language)
        let alert = NSAlert()
        alert.messageText = prompt.title
        alert.informativeText = prompt.message
        alert.addButton(withTitle: prompt.record)
        alert.addButton(withTitle: prompt.copyAndRecord)
        alert.addButton(withTitle: prompt.cancel).keyEquivalent = "\u{1b}"
        alert.showsSuppressionButton = true
        alert.suppressionButton?.title = prompt.dontAsk
        NSApplication.shared.activate(ignoringOtherApps: true)

        switch alert.runModal() {
        case .alertFirstButtonReturn:
            break
        case .alertSecondButtonReturn:
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(prompt.notice, forType: .string)
        default:
            return false
        }
        if alert.suppressionButton?.state == .on {
            defaults.set(true, forKey: MenuController.skipConsentPromptKey)
            rebuildMenu()
        }
        return true
    }

    @objc private func toggleConsentPrompt() {
        let defaults = UserDefaults.standard
        let key = MenuController.skipConsentPromptKey
        defaults.set(!defaults.bool(forKey: key), forKey: key)
        rebuildMenu()
    }

    private func loadDeleteAudioSetting() {
        DispatchQueue.global(qos: .utility).async { [self] in
            let state = MenuController.controlOutput(
                ["delete-audio"],
                projectURL: projectURL,
                pythonURL: pythonURL
            )
            DispatchQueue.main.async {
                self.deleteAudioEnabled = state.map { $0 == "on" }
                self.rebuildMenu()
            }
        }
    }

    @objc private func toggleDeleteAudio() {
        guard let enabled = deleteAudioEnabled else { return }
        DispatchQueue.global(qos: .utility).async { [self] in
            let state = MenuController.controlOutput(
                ["delete-audio", enabled ? "off" : "on"],
                projectURL: projectURL,
                pythonURL: pythonURL
            )
            DispatchQueue.main.async {
                if let state {
                    self.deleteAudioEnabled = state == "on"
                    self.rebuildMenu()
                } else {
                    self.showAlert(
                        title: "Could not change the setting",
                        message: "Check ~/.config/meetrec/config.json."
                    )
                }
            }
        }
    }

    @objc private func stopRecording() {
        guard let recorder = activeRecorder,
              let sessionURL = activeSessionURL
        else {
            return
        }
        setStatus(recording: false, text: "…")
        Task { @MainActor [weak self, recorder] in
            do {
                try await recorder.stop()
                self?.recordingFinished(
                    sessionURL: sessionURL,
                    status: 0
                )
            } catch {
                recorder.markFailed(error)
                self?.recordingFinished(
                    sessionURL: sessionURL,
                    status: 2
                )
            }
        }
    }

    private func recordingFinished(sessionURL: URL, status: Int32) {
        let preset = activePreset
            ?? RecordingPreset(language: "it", model: "medium")
        stopTimer()
        activeLog?.close()
        activeRecorder = nil
        activeLog = nil
        activeSessionURL = nil
        activePreset = nil
        startedAt = nil
        setStatus(recording: false, text: "")
        rebuildMenu()

        guard status == 0 else {
            showAlert(
                title: "Recording failed",
                message: "See \(sessionURL.path)/recorder.log"
            )
            return
        }
        enqueueAndTranscribe(sessionURL, preset: preset)
    }

    private func enqueueAndTranscribe(
        _ sessionURL: URL,
        preset: RecordingPreset
    ) {
        workerQueue.async { [self] in
            let enqueue = runControl([
                "enqueue",
                sessionURL.path,
                "--language",
                preset.language,
                "--model",
                preset.model,
            ])
            guard enqueue == 0 else {
                notify(
                    title: "MeetRec",
                    message: "Could not enqueue \(sessionURL.lastPathComponent)"
                )
                return
            }
            DispatchQueue.main.async {
                self.startProgressMonitoring(sessionURL)
            }
            let succeeded = runWorkerUntilSettled()
            DispatchQueue.main.async {
                self.pollProgress()
            }
            notify(
                title: "MeetRec",
                message: succeeded
                    ? "Transcript ready: \(sessionURL.lastPathComponent)"
                    : "Transcription needs attention: \(sessionURL.lastPathComponent)"
            )
        }
    }

    private func resumePendingTranscriptions() {
        workerQueue.async { [self] in
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                self.startProgressMonitoring(nil)
            }
            _ = runWorkerUntilSettled()
            DispatchQueue.main.async {
                self.pollProgress()
            }
        }
    }

    /// `worker --once` makes one attempt per job. A failed attempt leaves the
    /// job pending for a retry that nothing else would start, and the menu
    /// would sit on "Retrying" forever. Rerun the worker while a job is
    /// pending; it moves the job to failed once its attempts run out.
    private func runWorkerUntilSettled() -> Bool {
        var status = runControl(["worker", "--once"])
        var runs = 1
        while status != 0 && runs < 10 && hasPendingJob() {
            status = runControl(["worker", "--once"])
            runs += 1
        }
        return status == 0
    }

    private func hasPendingJob() -> Bool {
        let entries = (try? FileManager.default.contentsOfDirectory(
            at: recordingsURL,
            includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]
        )) ?? []
        return entries.contains { entry in
            let jobURL = entry.appendingPathComponent("job.json")
            guard
                let data = try? Data(contentsOf: jobURL),
                let job = try? JSONDecoder().decode(JobStatus.self, from: data)
            else {
                return false
            }
            return job.state == "pending"
        }
    }

    private func startProgressMonitoring(_ sessionURL: URL?) {
        let resolved = sessionURL ?? findActiveSession()
        guard let resolved else { return }
        processingSessionURL = resolved
        currentProgress = nil
        processingTimer?.invalidate()
        processingTimer = Timer.scheduledTimer(
            withTimeInterval: 0.5,
            repeats: true
        ) { [weak self] _ in
            self?.pollProgress()
        }
        rebuildMenu()
        pollProgress()
    }

    private func pollProgress() {
        guard let sessionURL = processingSessionURL else {
            if let active = findActiveSession() {
                startProgressMonitoring(active)
            }
            return
        }
        let jobURL = sessionURL.appendingPathComponent("job.json")
        guard
            let data = try? Data(contentsOf: jobURL),
            let job = try? JSONDecoder().decode(JobStatus.self, from: data)
        else {
            return
        }

        currentProgress = job.progress
        updateProgressUI()

        if job.state == "complete" {
            if let next = findActiveSession() {
                startProgressMonitoring(next)
            } else {
                finishProgress(success: true)
            }
        } else if job.state == "failed" {
            finishProgress(success: false)
        }
    }

    private func findActiveSession() -> URL? {
        guard
            let entries = try? FileManager.default.contentsOfDirectory(
                at: recordingsURL,
                includingPropertiesForKeys: [.isDirectoryKey],
                options: [.skipsHiddenFiles]
            )
        else {
            return nil
        }

        var pending: [URL] = []
        var processing: [URL] = []
        for entry in entries {
            let jobURL = entry.appendingPathComponent("job.json")
            guard
                let data = try? Data(contentsOf: jobURL),
                let job = try? JSONDecoder().decode(
                    JobStatus.self,
                    from: data
                )
            else {
                continue
            }
            if job.state == "processing" {
                processing.append(entry)
            } else if job.state == "pending" {
                pending.append(entry)
            }
        }
        return (processing.sorted { $0.path < $1.path }
            + pending.sorted { $0.path < $1.path }).first
    }

    private func updateProgressUI() {
        guard let progress = currentProgress else {
            progressLabel?.stringValue = "Preparing transcript…"
            progressIndicator?.isIndeterminate = true
            progressIndicator?.startAnimation(nil)
            if activeRecorder == nil {
                setStatus(recording: false, text: "…")
            }
            return
        }

        // The job always reports a fraction, even for steps that cannot
        // describe their own progress. Showing it keeps the menu bar readable
        // during those steps instead of collapsing to a bare ellipsis.
        let title = phaseTitle(progress.phase)
        let percent = Int((progress.fraction * 100).rounded())
        progressLabel?.stringValue = "\(title) — \(percent)%"
        if progress.indeterminate {
            progressIndicator?.isIndeterminate = true
            progressIndicator?.startAnimation(nil)
        } else {
            progressIndicator?.stopAnimation(nil)
            progressIndicator?.isIndeterminate = false
            progressIndicator?.doubleValue = progress.fraction
        }
        if activeRecorder == nil {
            setStatus(recording: false, text: "\(percent)%")
        }
        progressLabel?.toolTip = progress.detail
    }

    private func phaseTitle(_ phase: String) -> String {
        switch phase {
        case "queued":
            return "Queued"
        case "starting":
            return "Starting"
        case "loading_model":
            return "Loading faster-whisper"
        case "transcribing_microphone":
            return "Transcribing microphone"
        case "loading_diarizer":
            return "Loading pyannote"
        case "transcribing_system":
            return "Transcribing system audio"
        case "diarizing_system":
            return "Separating speakers"
        case "diarization_complete":
            return "Speakers separated"
        case "merging":
            return "Merging timelines"
        case "writing_outputs":
            return "Writing transcript"
        case "on_stop":
            return "Finishing up"
        case "complete":
            return "Transcript complete"
        case "retrying":
            return "Retrying"
        case "failed":
            return "Processing failed"
        default:
            return "Processing transcript"
        }
    }

    private func finishProgress(success: Bool) {
        processingTimer?.invalidate()
        processingTimer = nil
        processingSessionURL = nil
        currentProgress = nil
        progressLabel = nil
        progressIndicator = nil
        if activeRecorder == nil {
            setStatus(recording: false, text: success ? "✓" : "!")
        }
        rebuildMenu()
        if success {
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
                if self.activeRecorder == nil
                    && self.processingSessionURL == nil
                {
                    self.setStatus(recording: false, text: "")
                }
            }
        }
    }

    private func runControl(_ arguments: [String]) -> Int32 {
        let logURL = recordingsURL.appendingPathComponent(".menubar.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(
                atPath: logURL.path,
                contents: nil
            )
        }
        let log = FileHandle(forWritingAtPath: logURL.path)
        _ = try? log?.seekToEnd()
        let process = Process()
        process.executableURL = pythonURL
        process.arguments = ["-m", "meeting_recorder.control"] + arguments
        process.currentDirectoryURL = projectURL
        process.standardOutput = log
        process.standardError = log
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            try? log?.write(contentsOf: Data("\(error)\n".utf8))
            log?.closeFile()
            return 1
        }
        log?.closeFile()
        return process.terminationStatus
    }

    private func startTimer() {
        timer = Timer.scheduledTimer(
            withTimeInterval: 1,
            repeats: true
        ) { [weak self] _ in
            guard let self, let startedAt = self.startedAt else { return }
            let elapsed = Int(Date().timeIntervalSince(startedAt))
            self.setStatus(
                recording: true,
                text: String(
                    format: "%02d:%02d",
                    elapsed / 60,
                    elapsed % 60
                )
            )
        }
    }

    private func stopTimer() {
        timer?.invalidate()
        timer = nil
    }

    @objc private func openRecordings() {
        try? FileManager.default.createDirectory(
            at: recordingsURL,
            withIntermediateDirectories: true
        )
        NSWorkspace.shared.open(recordingsURL)
    }

    @objc private func runDoctor() {
        DispatchQueue.global(qos: .utility).async { [self] in
            let status = runControl(["doctor"])
            DispatchQueue.main.async {
                self.showAlert(
                    title: status == 0
                        ? "MeetRec doctor"
                        : "MeetRec needs attention",
                    message: "See \(self.recordingsURL.path)/.menubar.log"
                )
            }
        }
    }

    private func notify(title: String, message: String) {
        let script = "display notification \(shellQuoted(message)) with title \(shellQuoted(title))"
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = ["-e", script]
        try? process.run()
    }

    private func shellQuoted(_ value: String) -> String {
        "\"\(value.replacingOccurrences(of: "\"", with: "\\\""))\""
    }

    private func showAlert(title: String, message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.runModal()
    }

    @objc private func showAbout() {
        let info = Bundle.main.infoDictionary
        let shortVersion = (info?["CFBundleShortVersionString"] as? String)
            .flatMap { $0.isEmpty ? nil : $0 } ?? "dev"

        let paragraph = NSMutableParagraphStyle()
        paragraph.alignment = .center
        let baseAttributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: NSFont.smallSystemFontSize),
            .foregroundColor: NSColor.labelColor,
            .paragraphStyle: paragraph,
        ]
        let credits = NSMutableAttributedString(
            string: "Local meeting recorder and transcriber for macOS.\n"
                + "Project folder: \(projectURL.path)\n",
            attributes: baseAttributes
        )
        var linkAttributes = baseAttributes
        linkAttributes[.link] = URL(string: "https://github.com/frasal-test/meetrec")
        credits.append(
            NSAttributedString(
                string: "github.com/frasal-test/meetrec",
                attributes: linkAttributes
            )
        )

        let options: [NSApplication.AboutPanelOptionKey: Any] = [
            .applicationName: "MeetRec",
            .applicationVersion: shortVersion,
            // Left out, the panel reads CFBundleVersion itself and prints the
            // build number as "Version 0.9.1 (1)". MeetRec has no separate
            // build number, so an empty string keeps the line to the version.
            .version: "",
            .credits: credits,
        ]
        NSApplication.shared.activate(ignoringOtherApps: true)
        NSApplication.shared.orderFrontStandardAboutPanel(options: options)
    }

    @objc private func quit() {
        if activeRecorder != nil {
            showAlert(
                title: "Recording in progress",
                message: "Stop the recording before quitting."
            )
            return
        }
        NSApplication.shared.terminate(nil)
    }
}

/// Holds an exclusive lock for as long as the process lives. A file lock works
/// no matter how the app was started, which `NSRunningApplication` does not:
/// launchd runs the executable directly rather than through LaunchServices.
/// Two instances would put two icons in the menu bar and have two recorders
/// competing for the microphone.
private func acquireSingleInstanceLock() -> Bool {
    let supportURL = FileManager.default.urls(
        for: .applicationSupportDirectory,
        in: .userDomainMask
    ).first?.appendingPathComponent("MeetRec", isDirectory: true)
    guard let supportURL else { return true }
    try? FileManager.default.createDirectory(
        at: supportURL,
        withIntermediateDirectories: true
    )

    let lockURL = supportURL.appendingPathComponent("instance.lock")
    let descriptor = open(lockURL.path, O_CREAT | O_RDWR, 0o644)
    guard descriptor >= 0 else { return true }
    guard flock(descriptor, LOCK_EX | LOCK_NB) == 0 else {
        close(descriptor)
        return false
    }
    // The descriptor is deliberately never closed: the lock has to outlive
    // this function and is released by the kernel when the process exits.
    return true
}

@main
private struct MeetRecMenuApplication {
    static func main() {
        guard #available(macOS 13.0, *) else {
            fputs("macOS 13.0 or later is required\n", stderr)
            exit(1)
        }

        if CommandLine.arguments.dropFirst().first == "--doctor" {
            runPermissionDoctor()
        }

        guard acquireSingleInstanceLock() else {
            fputs("MeetRec is already running\n", stderr)
            exit(0)
        }

        let projectURL: URL
        if CommandLine.arguments.count >= 2 {
            projectURL = URL(
                fileURLWithPath: CommandLine.arguments[1],
                isDirectory: true
            )
        } else if Bundle.main.bundleURL.pathExtension == "app" {
            projectURL = Bundle.main.bundleURL.deletingLastPathComponent()
        } else {
            projectURL = URL(fileURLWithPath: CommandLine.arguments[0])
                .deletingLastPathComponent()
        }

        let application = NSApplication.shared
        application.setActivationPolicy(.accessory)
        let controller = MenuController(projectURL: projectURL)
        withExtendedLifetime(controller) {
            application.run()
        }
    }
}
