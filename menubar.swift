import AppKit
import Foundation
import UserNotifications

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
    let lastError: String?

    private enum CodingKeys: String, CodingKey {
        case state
        case progress
        case lastError = "last_error"
    }
}

/// Native notifications, so macOS attributes them to MeetRec (not to Script
/// Editor, as `osascript` did) and a click can open the related file.
/// Outside the .app bundle UNUserNotificationCenter traps, so there the
/// message goes to stderr instead.
final class Notifier: NSObject, UNUserNotificationCenterDelegate, @unchecked Sendable {
    static let shared = Notifier()

    private let lock = NSLock()
    private var logURL: URL?

    private var available: Bool { Bundle.main.bundleIdentifier != nil }

    /// What happens to notifications goes to the app's log: launched with
    /// `open`, the app's stderr goes nowhere.
    private func log(_ message: String) {
        lock.lock()
        defer { lock.unlock() }
        let line = Data("\(ISO8601DateFormatter().string(from: Date())) notifications: \(message)\n".utf8)
        guard let url = logURL else {
            FileHandle.standardError.write(line)
            return
        }
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        guard let handle = FileHandle(forWritingAtPath: url.path) else { return }
        defer { handle.closeFile() }
        _ = try? handle.seekToEnd()
        try? handle.write(contentsOf: line)
    }

    /// Call once at app start.
    func start(logURL: URL) {
        lock.lock()
        self.logURL = logURL
        lock.unlock()
        guard available else { return }
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { granted, error in
            if let error = error {
                self.log("authorization error: \(error.localizedDescription)")
            } else if !granted {
                self.log("not allowed: turn them on in System Settings → Notifications → MeetRec")
            }
        }
    }

    func post(title: String, message: String, openPath: String?) {
        guard available else {
            FileHandle.standardError.write(Data("\(title): \(message)\n".utf8))
            return
        }
        let center = UNUserNotificationCenter.current()
        center.getNotificationSettings { settings in
            if settings.authorizationStatus == .denied {
                self.log("not shown, disabled in System Settings: \(message)")
                return
            }
            if settings.alertSetting == .disabled {
                self.log("delivered without a banner (alert style None): \(message)")
            }
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = message
            content.sound = .default
            if let openPath = openPath {
                content.userInfo = ["open": openPath]
            }
            let request = UNNotificationRequest(
                identifier: UUID().uuidString,
                content: content,
                trigger: nil
            )
            center.add(request) { error in
                if let error = error {
                    self.log("could not post: \(error.localizedDescription)")
                }
            }
        }
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler:
            @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .list, .sound])
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        if let path = response.notification.request.content.userInfo["open"] as? String,
           FileManager.default.fileExists(atPath: path)
        {
            DispatchQueue.main.async {
                NSWorkspace.shared.open(URL(fileURLWithPath: path))
            }
        }
        completionHandler()
    }
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
    private let llmControl: LLMControl
    /// Recovery at launch and post-recording transcription both run
    /// `worker --once`. A serial queue keeps them from overlapping: the
    /// second run starts after the first finishes and picks up whatever it
    /// enqueued in the meantime.
    private let workerQueue = DispatchQueue(
        label: "meetrec.transcription-worker",
        qos: .utility
    )
    /// `llm-run` waits for the user (ASK_CHOICE, ASK_RETRY), so it must not
    /// share the queue of the transcription (flow.md, "Confine tra processi").
    /// Serial: a second summary waits for the first one's screens.
    private let llmQueue = DispatchQueue(
        label: "meetrec.llm-summary",
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
    /// Result of the startup check of the LLM flow; false until it answers.
    private var llmAvailable = false
    private var llmCheckFinished = false
    private var llmProblems: [String] = []
    private var llmProgressText: String?
    private var llmProgressLabel: NSTextField?
    private var llmProgressIndicator: NSProgressIndicator?
    private var lastSummaryURL: URL?
    private var lastEmergencyURL: URL?
    private var choiceController: LLMChoiceWindowController?
    private var settingsController: LLMSettingsController?

    init(projectURL: URL) {
        self.projectURL = projectURL
        let pythonURL = projectURL.appendingPathComponent(".venv/bin/python")
        self.pythonURL = pythonURL
        self.llmControl = LLMControl(
            projectURL: projectURL,
            pythonURL: pythonURL
        )
        self.recordingsURL = MenuController.resolveRecordingsURL(
            projectURL: projectURL,
            pythonURL: pythonURL
        )
        super.init()
        Notifier.shared.start(
            logURL: recordingsURL.appendingPathComponent(".menubar.log")
        )
        configureMenu()
        loadDeleteAudioSetting()
        runLLMStartupCheck()
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
        if llmProgressText != nil {
            addLLMProgressItem(to: menu)
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
        if lastSummaryURL != nil {
            menu.addItem(
                withTitle: "Open last summary",
                action: #selector(openLastSummary),
                keyEquivalent: ""
            ).target = self
        }
        if lastEmergencyURL != nil {
            menu.addItem(
                withTitle: "Open emergency summary",
                action: #selector(openEmergencySummary),
                keyEquivalent: ""
            ).target = self
        }
        menu.addItem(
            withTitle: "Run doctor",
            action: #selector(runDoctor),
            keyEquivalent: "d"
        ).target = self
        menu.addItem(
            withTitle: "Settings…",
            action: #selector(openSettings),
            keyEquivalent: ","
        ).target = self
        if llmCheckFinished && !llmAvailable {
            // Disabled (no action): a status line, not a command.
            let unavailable = menu.addItem(
                withTitle: "LLM summary unavailable — run doctor",
                action: nil,
                keyEquivalent: ""
            )
            unavailable.toolTip = llmProblems.joined(separator: "\n")
        }
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

    private func addLLMProgressItem(to menu: NSMenu) {
        let item = NSMenuItem()
        let view = NSView(frame: NSRect(x: 0, y: 0, width: 320, height: 58))
        let label = NSTextField(labelWithString: llmProgressText ?? "")
        label.frame = NSRect(x: 14, y: 32, width: 292, height: 18)
        label.font = NSFont.systemFont(ofSize: 12, weight: .medium)
        label.lineBreakMode = .byTruncatingTail

        // The call has no measurable progress: indeterminate, no percentage.
        let indicator = NSProgressIndicator(
            frame: NSRect(x: 14, y: 12, width: 292, height: 12)
        )
        indicator.style = .bar
        indicator.controlSize = .small
        indicator.isIndeterminate = true
        indicator.startAnimation(nil)

        view.addSubview(label)
        view.addSubview(indicator)
        item.view = view
        menu.addItem(item)
        llmProgressLabel = label
        llmProgressIndicator = indicator
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
            guard succeeded else {
                notify(
                    title: "MeetRec",
                    message: transcriptFailureMessage(sessionURL)
                )
                return
            }
            // The summary flow tells the user how it ended, so there is one
            // notification for the transcript, not two.
            startSummary(sessionURL)
        }
    }

    /// Stato: END_FATAL. A failed SAVE_TRANSCRIPT ends the job `failed` with
    /// `last_error` "TranscriptSaveError: <message>"; the message is already
    /// the text of ui.md. It is shown only once the attempts ran out (the
    /// job is `failed`, not `pending`).
    private func transcriptFailureMessage(_ sessionURL: URL) -> String {
        let prefix = "TranscriptSaveError:"
        if let data = try? Data(
               contentsOf: sessionURL.appendingPathComponent("job.json")
           ),
           let job = try? JSONDecoder().decode(JobStatus.self, from: data),
           job.state == "failed",
           let error = job.lastError,
           error.hasPrefix(prefix)
        {
            let message = error.dropFirst(prefix.count)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if !message.isEmpty {
                return message
            }
        }
        return "Transcription needs attention: \(sessionURL.lastPathComponent)"
    }

    /// Stato: precondizione, controllo all'avvio. Offline, off the main thread.
    private func runLLMStartupCheck() {
        llmQueue.async { [self] in
            let result = llmControl.startupCheck()
            DispatchQueue.main.async {
                self.llmAvailable = result.ok
                self.llmProblems = result.problems
                self.llmCheckFinished = true
                self.rebuildMenu()
            }
        }
    }

    /// Transition: SAVE_TRANSCRIPT ok -> CHECK_LENGTH, in the separate LLM
    /// process. Runs on `llmQueue`, never on the transcription queue.
    private func startSummary(_ sessionURL: URL) {
        llmQueue.async { [self] in
            // Queued after the startup check, so its answer is already in.
            let available = DispatchQueue.main.sync { self.llmAvailable }
            guard available else {
                notify(
                    title: "MeetRec",
                    message: "Transcript ready: \(sessionURL.lastPathComponent)"
                )
                return
            }
            let driver = LLMFlowDriver(
                control: llmControl,
                handlers: LLMFlowDriver.Handlers(
                    askChoice: { [weak self] screen, answer in
                        self?.presentChoice(screen, answer: answer)
                    },
                    askRetry: { message, answer in
                        answer(llmAskRetry(message: message))
                    },
                    progress: { [weak self] model in
                        self?.showLLMProgress("Summarizing with \(model)…")
                    }
                )
            )
            let result = driver.run(
                session: sessionURL,
                providers: LLMKeychain.providersWithKey()
            )
            DispatchQueue.main.async {
                self.finishSummary(result)
            }
        }
    }

    /// Stato: ASK_CHOICE.
    private func presentChoice(
        _ screen: LLMChoiceScreen,
        answer: @escaping (LLMChoiceAnswer) -> Void
    ) {
        let controller = LLMChoiceWindowController(screen: screen) { [weak self] result in
            self?.choiceController = nil
            answer(result)
        }
        choiceController = controller
        controller.show()
    }

    /// Stato: CALL_PROVIDER. Indeterminate, no interaction.
    private func showLLMProgress(_ text: String) {
        llmProgressText = text
        if let label = llmProgressLabel {
            label.stringValue = text
        } else {
            rebuildMenu()
        }
        if activeRecorder == nil && processingSessionURL == nil {
            setStatus(recording: false, text: "…")
        }
    }

    /// Outcomes DONE, END_NO_LLM, END_ERROR, END_SAVE_ERROR, END_FATAL: the
    /// message comes ready from Python (ui.md, "Messaggi di esito").
    private func finishSummary(_ result: LLMRunResult) {
        llmProgressText = nil
        llmProgressLabel = nil
        llmProgressIndicator = nil
        if activeRecorder == nil && processingSessionURL == nil {
            setStatus(recording: false, text: "")
        }
        let message: String
        switch result {
        case .outcome(let outcome):
            message = outcome.message.isEmpty
                ? "Summary failed: internal error"
                : outcome.message
            if outcome.kind == "DONE", let url = outcome.resultPath {
                lastSummaryURL = url
            } else if outcome.kind == "END_SAVE_ERROR",
                      let url = outcome.emergencyPath
            {
                lastEmergencyURL = url
            }
        case .startupCheckFailed:
            message = "Summary unavailable: startup check failed. "
                + "Run doctor"
        case .failed:
            // Never the raw output of the process.
            message = "Summary failed: internal error"
        }
        rebuildMenu()
        var openPath: String?
        if case .outcome(let outcome) = result {
            if outcome.kind == "DONE" {
                openPath = outcome.resultPath?.path
            } else if outcome.kind == "END_SAVE_ERROR" {
                openPath = outcome.emergencyPath?.path
            }
        }
        notify(title: "MeetRec", message: message, openPath: openPath)
    }

    @objc private func openLastSummary() {
        if let url = lastSummaryURL { NSWorkspace.shared.open(url) }
    }

    @objc private func openEmergencySummary() {
        if let url = lastEmergencyURL { NSWorkspace.shared.open(url) }
    }

    /// Reads the current state first, so the window is built with the
    /// providers of the model table and the saved settings.
    @objc private func openSettings() {
        if let controller = settingsController {
            controller.show()
            return
        }
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            let status = LLMSettingsStatus.fetch(llmControl)
            DispatchQueue.main.async {
                if let controller = self.settingsController {
                    controller.show()
                    return
                }
                guard let status else {
                    self.showAlert(
                        title: "Could not read the settings",
                        message: "Check the Python environment in .venv."
                    )
                    return
                }
                let controller = LLMSettingsController(
                    control: self.llmControl,
                    status: status
                )
                controller.onClose = { [weak self] in
                    self?.settingsController = nil
                }
                self.settingsController = controller
                controller.show()
            }
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
                    && self.llmProgressText == nil
                {
                    self.setStatus(recording: false, text: "")
                }
            }
        }
    }

    /// Like runControl, but also returns the output; the log still gets
    /// all of it.
    private func runControlCapturingOutput(_ arguments: [String]) -> (Int32, String) {
        let logURL = recordingsURL.appendingPathComponent(".menubar.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        let log = FileHandle(forWritingAtPath: logURL.path)
        _ = try? log?.seekToEnd()
        defer { log?.closeFile() }
        let pipe = Pipe()
        let process = Process()
        process.executableURL = pythonURL
        process.arguments = ["-m", "meeting_recorder.control"] + arguments
        process.currentDirectoryURL = projectURL
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
        } catch {
            try? log?.write(contentsOf: Data("\(error)\n".utf8))
            return (1, "")
        }
        // Read before waiting: a full pipe would block the child forever.
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        try? log?.write(contentsOf: data)
        return (process.terminationStatus, String(decoding: data, as: UTF8.self))
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
            let (status, output) = runControlCapturingOutput(["doctor"])
            // Only the check lines: the permissions note that follows them
            // describes this process, not MeetRec.app, and stays in the log.
            let checks = output
                .split(separator: "\n")
                .filter { $0.hasPrefix("✓") || $0.hasPrefix("✗") }
                .joined(separator: "\n")
            DispatchQueue.main.async {
                self.showDoctorResult(passed: status == 0, checks: checks)
            }
        }
    }

    private func showDoctorResult(passed: Bool, checks: String) {
        let logURL = recordingsURL.appendingPathComponent(".menubar.log")
        let alert = NSAlert()
        alert.messageText = passed ? "All checks passed" : "MeetRec needs attention"
        alert.informativeText = checks.isEmpty
            ? "Doctor did not report any checks. See the full log."
            : "Full output in \(logURL.path)"
        if !checks.isEmpty {
            let width: CGFloat = 520
            let height: CGFloat = 260
            let scroll = NSScrollView(
                frame: NSRect(x: 0, y: 0, width: width, height: height)
            )
            scroll.hasVerticalScroller = true
            scroll.borderType = .bezelBorder
            let text = NSTextView(
                frame: NSRect(x: 0, y: 0, width: width, height: height)
            )
            text.isEditable = false
            text.isSelectable = true
            text.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
            text.textContainerInset = NSSize(width: 6, height: 6)
            text.isVerticallyResizable = true
            text.autoresizingMask = [.width]
            text.textContainer?.widthTracksTextView = true
            text.string = checks
            if let storage = text.textStorage {
                // Failures in red so they stand out in a long list.
                var offset = 0
                for line in checks.components(separatedBy: "\n") {
                    let length = (line as NSString).length
                    if line.hasPrefix("✗") {
                        storage.addAttribute(
                            .foregroundColor,
                            value: NSColor.systemRed,
                            range: NSRange(location: offset, length: length)
                        )
                    }
                    offset += length + 1
                }
            }
            scroll.documentView = text
            alert.accessoryView = scroll
        }
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Open log")
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertSecondButtonReturn {
            NSWorkspace.shared.open(logURL)
        }
    }

    private func notify(title: String, message: String, openPath: String? = nil) {
        Notifier.shared.post(title: title, message: message, openPath: openPath)
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

        // Answering llm-run after it exited must fail the write, not kill the
        // app with SIGPIPE.
        signal(SIGPIPE, SIG_IGN)

        let application = NSApplication.shared
        application.setActivationPolicy(.accessory)
        let controller = MenuController(projectURL: projectURL)
        withExtendedLifetime(controller) {
            application.run()
        }
    }
}
