import AppKit
import Foundation
import Security

// The macOS side of the LLM summary flow (docs/flow/). The state machine lives
// in Python (`llm-run`); this file only draws the two choice screens, keeps the
// API keys in the Keychain and talks to the Python process through JSON lines.

// MARK: - Keychain

/// One generic-password item per provider. The key goes into the Keychain and,
/// from there, only to a Python process through its stdin pipe: never into
/// argv or the environment (both visible), a log or a notification.
enum LLMKeychain {
    static let service = "com.frasal.meetrec.llm"
    static let providers = ["anthropic", "openai", "oci"]

    private static func query(_ provider: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: provider,
        ]
    }

    /// Asks for the attributes only, never the secret, so Settings can show
    /// the state without macOS asking to unlock the item.
    static func hasKey(_ provider: String) -> Bool {
        var request = query(provider)
        request[kSecReturnAttributes as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        return SecItemCopyMatching(request as CFDictionary, &item) == errSecSuccess
    }

    static func key(_ provider: String) -> String? {
        var request = query(provider)
        request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(request as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data
        else {
            return nil
        }
        return String(data: data, encoding: .utf8)
    }

    /// Adds the item or, if it exists, replaces its value.
    @discardableResult
    static func save(_ provider: String, key: String) -> Bool {
        let data = Data(key.utf8)
        let update = SecItemUpdate(
            query(provider) as CFDictionary,
            [kSecValueData as String: data] as CFDictionary
        )
        if update == errSecSuccess { return true }
        guard update == errSecItemNotFound else { return false }
        var item = query(provider)
        item[kSecValueData as String] = data
        return SecItemAdd(item as CFDictionary, nil) == errSecSuccess
    }

    /// True when the item is gone afterwards, also if it never existed.
    @discardableResult
    static func remove(_ provider: String) -> Bool {
        let status = SecItemDelete(query(provider) as CFDictionary)
        return status == errSecSuccess || status == errSecItemNotFound
    }

    /// The keys of the providers that have one, for the first line of `llm-run`.
    static func storedKeys() -> [String: String] {
        var keys: [String: String] = [:]
        for provider in providers {
            if let key = key(provider), !key.isEmpty {
                keys[provider] = key
            }
        }
        return keys
    }
}

// MARK: - Python control commands

struct LLMControl: Sendable {
    let projectURL: URL
    let pythonURL: URL

    func makeProcess(_ arguments: [String]) -> Process {
        let process = Process()
        process.executableURL = pythonURL
        process.arguments = ["-m", "meeting_recorder.control"] + arguments
        process.currentDirectoryURL = projectURL
        return process
    }

    /// Runs a control command to its end and returns its exit status and
    /// stdout; nil when it cannot be launched. Blocking: never call it on the
    /// main thread (the first key check installs the SDKs with pip).
    /// `input` reaches the process through stdin, which is how a key travels.
    /// stderr is dropped: nothing there is needed, and nothing may leak.
    func run(
        _ arguments: [String],
        input: String? = nil
    ) -> (status: Int32, output: Data)? {
        let process = makeProcess(arguments)
        let output = Pipe()
        let stdin = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        process.standardInput = input == nil ? FileHandle.nullDevice : stdin
        do {
            try process.run()
        } catch {
            return nil
        }
        if let input {
            try? stdin.fileHandleForWriting.write(contentsOf: Data(input.utf8))
            try? stdin.fileHandleForWriting.close()
        }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return (process.terminationStatus, data)
    }

    func runJSON(
        _ arguments: [String],
        input: String? = nil
    ) -> (status: Int32, object: [String: Any])? {
        guard let result = run(arguments, input: input),
              let object = try? JSONSerialization.jsonObject(with: result.output)
                  as? [String: Any]
        else {
            return nil
        }
        return (result.status, object)
    }

    /// Stato: precondizione, controllo all'avvio. Offline; the flow does not
    /// start when it fails.
    func startupCheck() -> (ok: Bool, problems: [String]) {
        guard let result = runJSON(["startup-check"]) else {
            return (false, ["startup-check could not run"])
        }
        let problems = (result.object["problems"] as? [String]) ?? []
        let ok = (result.object["ok"] as? Bool) == true && result.status == 0
        return (ok, problems)
    }
}

// MARK: - Protocol with `llm-run`

struct LLMChoiceScreen: Decodable {
    struct NoneOption: Decodable {
        let label: String
        let hint: String
    }

    struct Option: Decodable {
        let modelId: String
        let label: String
        let maxCost: String?
        let enabled: Bool
        let disabledReason: String?
    }

    struct ResponseLanguage: Decodable {
        let code: String
        let label: String
    }

    let transcriptName: String
    let language: String
    let wordCount: Int
    let noneOption: NoneOption
    let options: [Option]
    let responseLanguages: [ResponseLanguage]
}

/// The answer to ASK_CHOICE. `modelId == nil` is "None".
struct LLMChoiceAnswer {
    let modelId: String?
    let responseLanguage: String?

    static let none = LLMChoiceAnswer(modelId: nil, responseLanguage: nil)
}

struct LLMOutcome {
    let kind: String
    let message: String
    let resultPath: URL?
    let emergencyPath: URL?
}

enum LLMRunResult {
    case outcome(LLMOutcome)
    /// The process refused to start the flow (exit 20).
    case startupCheckFailed
    /// No outcome: protocol error, internal error, crash, empty output.
    case failed
}

private struct LLMEvent: Decodable {
    let event: String
    let screen: LLMChoiceScreen?
    let message: String?
    let model: String?
    let kind: String?
    let resultPath: String?
    let emergencyPath: String?
}

/// Splits what a pipe delivers into lines. Blocks in `next()`.
private struct LineReader {
    let handle: FileHandle
    private var buffer = Data()

    init(handle: FileHandle) {
        self.handle = handle
    }

    mutating func next() -> Data? {
        while true {
            if let newline = buffer.firstIndex(of: 0x0A) {
                let line = Data(buffer[buffer.startIndex..<newline])
                buffer.removeSubrange(buffer.startIndex...newline)
                return line
            }
            let chunk = handle.availableData
            if chunk.isEmpty {
                guard !buffer.isEmpty else { return nil }
                let rest = buffer
                buffer = Data()
                return rest
            }
            buffer.append(chunk)
        }
    }
}

private final class AnswerBox<Value>: @unchecked Sendable {
    var value: Value?
}

/// Runs `llm-run` for one session. The screens are the caller's: `askChoice`,
/// `askRetry` and `progress` are called on the main thread, while `run`
/// itself blocks a background thread until the process ends. The transcription
/// queue never waits for it (flow.md, "Confine tra processi").
final class LLMFlowDriver {
    struct Handlers {
        /// Stato: ASK_CHOICE. Must call the answer exactly once.
        var askChoice: (LLMChoiceScreen, @escaping (LLMChoiceAnswer) -> Void) -> Void
        /// Stato: ASK_RETRY. Must call the answer exactly once.
        var askRetry: (String, @escaping (Bool) -> Void) -> Void
        /// Stato: CALL_PROVIDER, with the model label.
        var progress: (String) -> Void
    }

    private let control: LLMControl
    private let handlers: Handlers

    init(control: LLMControl, handlers: Handlers) {
        self.control = control
        self.handlers = handlers
    }

    /// Asks the main thread for an answer and waits for it here.
    private func ask<Value>(
        _ present: @escaping (@escaping (Value) -> Void) -> Void
    ) -> Value? {
        let box = AnswerBox<Value>()
        let semaphore = DispatchSemaphore(value: 0)
        DispatchQueue.main.async {
            present { value in
                box.value = value
                semaphore.signal()
            }
        }
        semaphore.wait()
        return box.value
    }

    func run(session: URL, keys: [String: String]) -> LLMRunResult {
        let process = control.makeProcess(["llm-run", session.path])
        let input = Pipe()
        let output = Pipe()
        process.standardInput = input
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
        } catch {
            return .failed
        }

        let writer = input.fileHandleForWriting
        var writerOpen = true
        func send(_ object: [String: Any]) {
            guard writerOpen,
                  var data = try? JSONSerialization.data(withJSONObject: object)
            else {
                return
            }
            data.append(0x0A)
            try? writer.write(contentsOf: data)
        }
        func closeInput() {
            guard writerOpen else { return }
            writerOpen = false
            try? writer.close()
        }

        // First line: the keys, only the providers that have one.
        send(["type": "keys", "keys": keys])

        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        var reader = LineReader(handle: output.fileHandleForReading)
        var outcome: LLMOutcome?
        var startupFailed = false

        while let line = reader.next() {
            guard let event = try? decoder.decode(LLMEvent.self, from: line) else {
                continue
            }
            switch event.event {
            case "ask_choice":
                var answer = LLMChoiceAnswer.none
                if let screen = event.screen {
                    answer = ask { done in
                        self.handlers.askChoice(screen, done)
                    } ?? .none
                }
                send([
                    "model_id": answer.modelId.map { $0 as Any } ?? NSNull(),
                    "response_language": answer.responseLanguage.map { $0 as Any }
                        ?? NSNull(),
                ])
            case "ask_retry":
                let retry: Bool = ask { done in
                    self.handlers.askRetry(event.message ?? "", done)
                } ?? false
                send(["retry": retry])
            case "progress":
                if let model = event.model {
                    DispatchQueue.main.async { self.handlers.progress(model) }
                }
            case "outcome":
                outcome = LLMOutcome(
                    kind: event.kind ?? "",
                    message: event.message ?? "",
                    resultPath: event.resultPath.map { URL(fileURLWithPath: $0) },
                    emergencyPath: event.emergencyPath.map { URL(fileURLWithPath: $0) }
                )
                // Nothing more to answer: let the process finish.
                closeInput()
            case "startup_check_failed":
                startupFailed = true
            default:
                // protocol_error, error: no outcome follows, which is the
                // generic failure. The detail is never shown.
                break
            }
        }
        closeInput()
        process.waitUntilExit()

        if let outcome {
            return .outcome(outcome)
        }
        return startupFailed ? .startupCheckFailed : .failed
    }
}

// MARK: - Small UI helpers

private func makeLabel(
    _ text: String,
    size: CGFloat = 13,
    weight: NSFont.Weight = .regular,
    color: NSColor = .labelColor
) -> NSTextField {
    let label = NSTextField(labelWithString: text)
    label.font = NSFont.systemFont(ofSize: size, weight: weight)
    label.textColor = color
    return label
}

private func makeSpacer() -> NSView {
    let spacer = NSView()
    spacer.setContentHuggingPriority(NSLayoutConstraint.Priority(1), for: .horizontal)
    return spacer
}

private func fixWidth(_ view: NSView, _ width: CGFloat) {
    view.translatesAutoresizingMaskIntoConstraints = false
    view.widthAnchor.constraint(equalToConstant: width).isActive = true
}

/// Radio buttons that exclude each other without sharing a superview, and
/// that all start off: nothing is preselected (I5).
final class ExclusiveRadioGroup: NSObject {
    private(set) var buttons: [NSButton] = []
    var onSelect: ((Int) -> Void)?

    var selectedIndex: Int? {
        buttons.firstIndex { $0.state == .on }
    }

    func addRadio(title: String) -> NSButton {
        let button = NSButton(
            radioButtonWithTitle: title,
            target: self,
            action: #selector(clicked(_:))
        )
        button.state = .off
        buttons.append(button)
        return button
    }

    func select(_ index: Int?) {
        for (position, button) in buttons.enumerated() {
            button.state = position == index ? .on : .off
        }
    }

    func removeAll() {
        buttons = []
    }

    @objc private func clicked(_ sender: NSButton) {
        guard let index = buttons.firstIndex(of: sender) else { return }
        select(index)
        onSelect?(index)
    }
}

/// A menu-bar app has no main menu, so Cmd-V does nothing in a text field.
/// Pasting a key is the whole point of Settings, hence this Edit menu.
private func installEditMenuIfNeeded() {
    guard NSApp.mainMenu == nil else { return }
    let mainMenu = NSMenu()
    let editItem = NSMenuItem()
    mainMenu.addItem(editItem)
    let edit = NSMenu(title: "Edit")
    edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
    edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
    edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
    edit.addItem(
        withTitle: "Select All",
        action: #selector(NSText.selectAll(_:)),
        keyEquivalent: "a"
    )
    editItem.submenu = edit
    NSApp.mainMenu = mainMenu
}

// MARK: - ASK_CHOICE

/// Stato: ASK_CHOICE. The single screen of ui.md: model (or "None") and
/// response language. Nothing is preselected and nothing is remembered (I5);
/// there is no default button, so Return does nothing.
final class LLMChoiceWindowController: NSObject, NSWindowDelegate {
    private static let contentWidth: CGFloat = 420

    private let window: NSWindow
    private let models = ExclusiveRadioGroup()
    /// Parallel to `models.buttons`; nil is "None".
    private var modelIDs: [String?] = []
    private let languages = NSSegmentedControl()
    private var languageCodes: [String] = []
    private var languageIndex: Int?
    private let actionButton = NSButton()
    private var completion: ((LLMChoiceAnswer) -> Void)?

    init(screen: LLMChoiceScreen, completion: @escaping (LLMChoiceAnswer) -> Void) {
        self.completion = completion
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 460, height: 300),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        super.init()
        window.title = "MeetRec"
        window.isReleasedWhenClosed = false
        window.delegate = self
        buildContent(screen)
    }

    private func buildContent(_ screen: LLMChoiceScreen) {
        let width = LLMChoiceWindowController.contentWidth
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 6
        root.edgeInsets = NSEdgeInsets(top: 20, left: 20, bottom: 20, right: 20)

        func add(_ view: NSView, spacingAfter: CGFloat? = nil) {
            root.addArrangedSubview(view)
            if let spacingAfter { root.setCustomSpacing(spacingAfter, after: view) }
        }

        add(makeLabel("Transcript saved", size: 16, weight: .semibold))
        let subtitle = makeLabel(
            "\(screen.transcriptName) · \(screen.language) · "
                + "~\(screen.wordCount) words",
            size: 12,
            color: .secondaryLabelColor
        )
        subtitle.lineBreakMode = .byTruncatingMiddle
        fixWidth(subtitle, width)
        add(subtitle, spacingAfter: 16)

        add(makeLabel("Summarize with", size: 12, weight: .semibold), spacingAfter: 4)
        modelIDs.append(nil)
        add(
            optionRow(
                title: screen.noneOption.label,
                note: screen.noneOption.hint,
                enabled: true
            )
        )
        for option in screen.options {
            modelIDs.append(option.enabled ? option.modelId : nil)
            let note = option.enabled
                ? option.maxCost
                : option.disabledReason
            add(
                optionRow(
                    title: option.label,
                    note: note,
                    enabled: option.enabled
                )
            )
        }
        if let last = root.arrangedSubviews.last {
            root.setCustomSpacing(18, after: last)
        }
        models.onSelect = { [weak self] _ in self?.refreshControls() }

        add(makeLabel("Response language", size: 12, weight: .semibold), spacingAfter: 4)
        languages.segmentCount = screen.responseLanguages.count
        for (index, language) in screen.responseLanguages.enumerated() {
            languages.setLabel(language.label, forSegment: index)
            languages.setSelected(false, forSegment: index)
            languageCodes.append(language.code)
        }
        languages.trackingMode = .selectOne
        languages.segmentStyle = .rounded
        languages.selectedSegment = -1
        languages.target = self
        languages.action = #selector(languageChanged(_:))
        add(languages, spacingAfter: 20)

        actionButton.bezelStyle = .rounded
        actionButton.target = self
        actionButton.action = #selector(actionTapped)
        // No key equivalent on purpose: Return must not start anything.
        actionButton.keyEquivalent = ""
        let buttons = NSStackView(views: [makeSpacer(), actionButton])
        buttons.orientation = .horizontal
        fixWidth(buttons, width)
        add(buttons)

        let content = NSView()
        window.contentView = content
        root.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            root.topAnchor.constraint(equalTo: content.topAnchor),
            root.bottomAnchor.constraint(equalTo: content.bottomAnchor),
        ])
        window.defaultButtonCell = nil
        refreshControls()
        content.layoutSubtreeIfNeeded()
        window.setContentSize(root.fittingSize)
    }

    private func optionRow(title: String, note: String?, enabled: Bool) -> NSView {
        let radio = models.addRadio(title: title)
        radio.isEnabled = enabled
        let noteLabel = makeLabel(note ?? "", size: 11, color: .secondaryLabelColor)
        noteLabel.alignment = .right
        noteLabel.setContentCompressionResistancePriority(.required, for: .horizontal)
        // The spacer pushes the note to the right edge.
        let row = NSStackView(views: [radio, makeSpacer(), noteLabel])
        row.orientation = .horizontal
        row.alignment = .firstBaseline
        row.spacing = 10
        fixWidth(row, LLMChoiceWindowController.contentWidth)
        return row
    }

    private var noneSelected: Bool {
        models.selectedIndex == 0
    }

    /// "None" turns the language row off and "Start" into "Close";
    /// "Start" needs a model and a language.
    private func refreshControls() {
        let selected = models.selectedIndex
        let none = selected == 0
        languages.isEnabled = !none
        actionButton.title = none ? "Close" : "Start"
        actionButton.isEnabled = none || (selected != nil && languageIndex != nil)
    }

    @objc private func languageChanged(_ sender: NSSegmentedControl) {
        languageIndex = sender.selectedSegment >= 0 ? sender.selectedSegment : nil
        refreshControls()
    }

    @objc private func actionTapped() {
        guard let selected = models.selectedIndex else { return }
        if noneSelected {
            finish(.none)
        } else if let modelID = modelIDs[selected], let language = languageIndex {
            finish(
                LLMChoiceAnswer(
                    modelId: modelID,
                    responseLanguage: languageCodes[language]
                )
            )
        }
    }

    private func finish(_ answer: LLMChoiceAnswer) {
        guard let completion else { return }
        self.completion = nil
        window.close()
        completion(answer)
    }

    /// Closing the window with the close box is read as "None": the flow
    /// needs an answer, and not choosing a model is the only reading that
    /// cannot send the transcript to a provider by accident (END_NO_LLM,
    /// `declined`). The transcript is already saved either way.
    func windowWillClose(_ notification: Notification) {
        finish(.none)
    }

    func show() {
        NSApp.activate(ignoringOtherApps: true)
        window.center()
        window.makeKeyAndOrderFront(nil)
    }
}

// MARK: - ASK_RETRY

/// Stato: ASK_RETRY. Message with "Retry" and "Cancel" and no default: Return
/// and Escape do nothing, the user has to click (I10 makes the second attempt
/// their decision). True = Retry.
func llmAskRetry(message: String) -> Bool {
    let alert = NSAlert()
    alert.messageText = message
    let retry = alert.addButton(withTitle: "Retry")
    let leave = alert.addButton(withTitle: "Cancel")
    retry.keyEquivalent = ""
    leave.keyEquivalent = ""
    alert.window.defaultButtonCell = nil
    NSApp.activate(ignoringOtherApps: true)
    return alert.runModal() == .alertFirstButtonReturn
}

// MARK: - Settings

struct LLMSettingsStatus: Decodable {
    struct Model: Decodable {
        let provider: String
        let modelId: String
        let label: String
    }

    let automatic: Bool
    let defaultModel: String?
    let ociRegion: String?
    let models: [Model]

    /// Providers in model-table order.
    var providers: [String] {
        var seen: [String] = []
        for model in models where !seen.contains(model.provider) {
            seen.append(model.provider)
        }
        return seen
    }

    func provider(ofModel modelID: String?) -> String? {
        models.first { $0.modelId == modelID }?.provider
    }

    static func fetch(_ control: LLMControl) -> LLMSettingsStatus? {
        guard let result = control.run(["auto-summary", "status"]),
              result.status == 0
        else {
            return nil
        }
        return decode(result.output)
    }

    static func decode(_ data: Data) -> LLMSettingsStatus? {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try? decoder.decode(LLMSettingsStatus.self, from: data)
    }
}

private final class LLMKeyRow {
    let provider: String
    let name: String
    let field = NSSecureTextField()
    let saveButton = NSButton(title: "Save", target: nil, action: nil)
    let removeButton = NSButton(title: "Remove", target: nil, action: nil)
    let statusLabel = makeLabel("", size: 12, color: .secondaryLabelColor)
    let spinner = NSProgressIndicator()
    var regionPopup: NSPopUpButton?
    var view = NSStackView()
    var busy = false

    init(provider: String, name: String) {
        self.provider = provider
        self.name = name
    }
}

/// Settings, outside the flow (ui.md, "Settings"). Keys go to the Keychain
/// after the verification of `llm-key-verify`; Automatic Summary is changed
/// only through `auto-summary`, with the model the user picked.
final class LLMSettingsController: NSObject, NSWindowDelegate {
    private static let providerNames = [
        "anthropic": "Anthropic",
        "openai": "OpenAI",
        "oci": "Oracle OCI",
    ]
    /// Keep in sync with OCI_SUPPORTED_REGIONS in
    /// meeting_recorder/llm/adapters/openai_compat.py.
    static let ociRegions = [
        "eu-frankfurt-1",
        "ap-hyderabad-1",
        "ap-osaka-1",
        "us-ashburn-1",
        "us-chicago-1",
        "us-phoenix-1",
    ]
    private static let regionPlaceholder = "Choose…"
    private static let contentWidth: CGFloat = 540

    private let control: LLMControl
    private var status: LLMSettingsStatus
    private let window: NSWindow
    private let root = NSStackView()
    static let privacyNotice = "With an LLM, the meeting text (never the audio) is "
        + "sent to the service you choose: Anthropic, OpenAI or Oracle. From then on, "
        + "your data is governed by that service's terms of use, privacy policy and "
        + "any agreements you have with it (SLA, DPA), not by MeetRec. Check them "
        + "before using it for confidential meetings."
    private var rows: [LLMKeyRow] = []
    private let autoCheckbox = NSButton(
        checkboxWithTitle: "Automatic Summary",
        target: nil,
        action: nil
    )
    private let autoHint = makeLabel(
        "Save an API key first to turn it on.",
        size: 11,
        color: .secondaryLabelColor
    )
    private let autoModelsRow = NSStackView()
    private let autoModels = ExclusiveRadioGroup()
    private var autoModelIDs: [String] = []
    /// The checkbox as the user sees it. It can be ticked while the setting is
    /// still off: it only becomes effective when a model is chosen.
    private var autoWanted: Bool
    private var autoBusy = false
    var onClose: (() -> Void)?

    init(control: LLMControl, status: LLMSettingsStatus) {
        self.control = control
        self.status = status
        autoWanted = status.automatic
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 580, height: 300),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        super.init()
        window.title = "MeetRec Settings"
        window.isReleasedWhenClosed = false
        window.delegate = self
        buildContent()
    }

    // MARK: Layout

    private func buildContent() {
        let width = LLMSettingsController.contentWidth
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 10
        root.edgeInsets = NSEdgeInsets(top: 20, left: 20, bottom: 20, right: 20)

        root.addArrangedSubview(makeLabel("API key", size: 13, weight: .semibold))
        // Always on screen, not only in the alert before the first key: the
        // user should know where the text goes every time a key is managed.
        let notice = NSTextField(wrappingLabelWithString: Self.privacyNotice)
        notice.font = NSFont.systemFont(ofSize: 11)
        notice.textColor = .secondaryLabelColor
        fixWidth(notice, width)
        root.addArrangedSubview(notice)
        for provider in status.providers {
            let row = makeRow(provider: provider, index: rows.count)
            rows.append(row)
            root.addArrangedSubview(row.view)
        }

        let separator = NSBox()
        separator.boxType = .separator
        fixWidth(separator, width)
        root.addArrangedSubview(separator)

        autoCheckbox.target = self
        autoCheckbox.action = #selector(autoToggled)
        root.addArrangedSubview(autoCheckbox)
        root.addArrangedSubview(autoHint)
        autoModelsRow.orientation = .horizontal
        autoModelsRow.alignment = .firstBaseline
        autoModelsRow.spacing = 14
        autoModelsRow.edgeInsets = NSEdgeInsets(top: 0, left: 20, bottom: 0, right: 0)
        root.addArrangedSubview(autoModelsRow)
        autoModels.onSelect = { [weak self] index in self?.autoModelChosen(index) }

        let content = NSView()
        window.contentView = content
        root.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            root.topAnchor.constraint(equalTo: content.topAnchor),
            root.bottomAnchor.constraint(equalTo: content.bottomAnchor),
        ])
        refreshAll()
        content.layoutSubtreeIfNeeded()
        window.setContentSize(root.fittingSize)
    }

    private func makeRow(provider: String, index: Int) -> LLMKeyRow {
        let name = LLMSettingsController.providerNames[provider] ?? provider
        let row = LLMKeyRow(provider: provider, name: name)

        let nameLabel = makeLabel(name)
        fixWidth(nameLabel, 90)
        fixWidth(row.field, 210)
        var first: [NSView] = [nameLabel, row.field]

        if provider == "oci" {
            let popup = NSPopUpButton(frame: .zero, pullsDown: false)
            popup.addItem(withTitle: LLMSettingsController.regionPlaceholder)
            popup.addItems(withTitles: LLMSettingsController.ociRegions)
            // No region unless one was saved: the placeholder stays selected.
            if let saved = status.ociRegion,
               let position = LLMSettingsController.ociRegions.firstIndex(of: saved)
            {
                popup.selectItem(at: position + 1)
            } else {
                popup.selectItem(at: 0)
            }
            fixWidth(popup, 150)
            row.regionPopup = popup
            first += [makeLabel("Region"), popup]
        }
        let top = NSStackView(views: first)
        top.orientation = .horizontal
        top.alignment = .centerY
        top.spacing = 8

        row.saveButton.target = self
        row.saveButton.action = #selector(saveTapped(_:))
        row.saveButton.tag = index
        row.removeButton.target = self
        row.removeButton.action = #selector(removeTapped(_:))
        row.removeButton.tag = index
        row.spinner.style = .spinning
        row.spinner.controlSize = .small
        row.spinner.isIndeterminate = true
        row.spinner.isDisplayedWhenStopped = false
        let indent = NSView()
        fixWidth(indent, 90)
        let bottom = NSStackView(views: [
            indent, row.saveButton, row.removeButton, row.spinner, row.statusLabel,
        ])
        bottom.orientation = .horizontal
        bottom.alignment = .centerY
        bottom.spacing = 8

        let stack = NSStackView(views: [top, bottom])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 6
        row.view = stack
        return row
    }

    // MARK: State

    private func refreshAll() {
        for row in rows {
            refreshRow(row)
        }
        refreshAutomatic()
    }

    private func refreshRow(_ row: LLMKeyRow) {
        let has = LLMKeychain.hasKey(row.provider)
        let enabled = !row.busy
        row.field.isEnabled = enabled
        row.saveButton.isEnabled = enabled
        row.removeButton.isEnabled = enabled
        row.regionPopup?.isEnabled = enabled
        row.removeButton.isHidden = !has
        row.field.placeholderString = has ? "••••••••" : nil
        if row.busy {
            row.statusLabel.stringValue = "Verifying…"
            row.statusLabel.textColor = .secondaryLabelColor
            row.spinner.startAnimation(nil)
        } else {
            row.spinner.stopAnimation(nil)
            row.statusLabel.stringValue = has ? "✓" : ""
            row.statusLabel.textColor = .systemGreen
            row.statusLabel.setAccessibilityLabel(has ? "Key saved" : nil)
        }
    }

    private func providersWithKey() -> Set<String> {
        Set(status.providers.filter { LLMKeychain.hasKey($0) })
    }

    /// The checkbox and the model choice, from the Keychain and the status.
    private func refreshAutomatic() {
        let withKey = providersWithKey()
        let available = status.models.filter { withKey.contains($0.provider) }
        let ticked = autoWanted && !available.isEmpty
        autoCheckbox.isEnabled = !available.isEmpty && !autoBusy
        autoCheckbox.state = ticked ? .on : .off
        autoHint.isHidden = !available.isEmpty

        for view in autoModelsRow.arrangedSubviews {
            autoModelsRow.removeArrangedSubview(view)
            view.removeFromSuperview()
        }
        autoModels.removeAll()
        autoModelIDs = []
        if ticked {
            autoModelsRow.addArrangedSubview(makeLabel("Model:", size: 12))
            for model in available {
                let radio = autoModels.addRadio(title: model.label)
                radio.isEnabled = !autoBusy
                autoModelsRow.addArrangedSubview(radio)
                autoModelIDs.append(model.modelId)
            }
            // The only radio that starts on is the user's own saved choice.
            if status.automatic,
               let saved = status.defaultModel,
               let position = autoModelIDs.firstIndex(of: saved)
            {
                autoModels.select(position)
            }
        }
        autoModelsRow.isHidden = !ticked
        fitWindow()
    }

    private func fitWindow() {
        window.contentView?.layoutSubtreeIfNeeded()
        let size = root.fittingSize
        let old = window.frame
        var frame = window.frameRect(forContentRect: NSRect(origin: .zero, size: size))
        frame.origin = NSPoint(x: old.minX, y: old.maxY - frame.height)
        window.setFrame(frame, display: window.isVisible)
    }

    private func reloadStatus() {
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            let fresh = LLMSettingsStatus.fetch(control)
            DispatchQueue.main.async {
                if let fresh { self.status = fresh }
                self.refreshAll()
            }
        }
    }

    private func presentAlert(title: String, message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.beginSheetModal(for: window, completionHandler: nil)
    }

    // MARK: Keys

    @objc private func saveTapped(_ sender: NSButton) {
        guard rows.indices.contains(sender.tag) else { return }
        let row = rows[sender.tag]
        let key = row.field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !key.isEmpty else {
            presentAlert(
                title: "Enter the API key",
                message: "Paste your \(row.name) key into the field, then click Save."
            )
            return
        }
        var region: String?
        if let popup = row.regionPopup {
            guard popup.indexOfSelectedItem > 0, let title = popup.titleOfSelectedItem else {
                presentAlert(
                    title: "Choose the region",
                    message: "Choose the region of your Oracle OCI key."
                )
                return
            }
            region = title
        }

        // Before the first key of a service: the user learns where the text goes.
        guard !LLMKeychain.hasKey(row.provider) else {
            verifyAndSave(row, key: key, region: region)
            return
        }
        let alert = NSAlert()
        alert.messageText = "The meeting text (not the audio) will be sent to "
            + "\(row.name) to create the summary."
        alert.informativeText = "That text is governed by the terms of use and "
            + "privacy policy of \(row.name) (or by your company's agreement with "
            + "the service), not by MeetRec."
        let proceed = alert.addButton(withTitle: "Continue")
        let cancel = alert.addButton(withTitle: "Cancel")
        proceed.keyEquivalent = ""
        cancel.keyEquivalent = ""
        alert.window.defaultButtonCell = nil
        alert.beginSheetModal(for: window) { [self] response in
            if response == .alertFirstButtonReturn {
                verifyAndSave(row, key: key, region: region)
            }
        }
    }

    /// The first check can take minutes: it installs the SDKs with pip. It
    /// runs off the main thread, the row shows a spinner. The key reaches
    /// Python on stdin; it is saved only if the check says ok (I4).
    private func verifyAndSave(_ row: LLMKeyRow, key: String, region: String?) {
        row.busy = true
        refreshRow(row)
        var arguments = ["llm-key-verify", "--provider", row.provider]
        if let region { arguments += ["--region", region] }
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            let result = control.runJSON(arguments, input: key + "\n")?.object
            DispatchQueue.main.async {
                row.busy = false
                if result?["ok"] as? Bool == true {
                    if LLMKeychain.save(row.provider, key: key) {
                        row.field.stringValue = ""
                    } else {
                        self.presentAlert(
                            title: "Key not saved",
                            message: "Could not write to the Keychain."
                        )
                    }
                } else {
                    let message = (result?["message"] as? String)
                        ?? "Verification failed: internal error."
                    self.presentAlert(title: "Key not saved", message: message)
                }
                self.refreshAll()
                self.reloadStatus()
            }
        }
    }

    @objc private func removeTapped(_ sender: NSButton) {
        guard rows.indices.contains(sender.tag) else { return }
        let row = rows[sender.tag]
        guard LLMKeychain.remove(row.provider) else {
            presentAlert(
                title: "Key not removed",
                message: "Could not delete the item from the Keychain."
            )
            return
        }
        row.field.stringValue = ""
        // A default model with no key behind it would send every summary
        // to ASK_CHOICE: turn the automatic mode off with it.
        if status.provider(ofModel: status.defaultModel) == row.provider {
            autoWanted = false
            runAutoSummary(["off"])
        } else {
            refreshAll()
        }
    }

    // MARK: Automatic Summary

    @objc private func autoToggled() {
        autoWanted = autoCheckbox.state == .on
        if autoWanted {
            // Effective only once a model is chosen.
            refreshAutomatic()
        } else if status.automatic || status.defaultModel != nil {
            runAutoSummary(["off"])
        } else {
            refreshAutomatic()
        }
    }

    private func autoModelChosen(_ index: Int) {
        guard autoModelIDs.indices.contains(index) else { return }
        runAutoSummary(["on", "--model", autoModelIDs[index]])
    }

    private func runAutoSummary(_ arguments: [String]) {
        autoBusy = true
        refreshAutomatic()
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            let result = control.run(["auto-summary"] + arguments)
            let fresh = result.flatMap { LLMSettingsStatus.decode($0.output) }
            DispatchQueue.main.async {
                self.autoBusy = false
                if let fresh, result?.status == 0 {
                    self.status = fresh
                    self.autoWanted = fresh.automatic
                } else {
                    self.presentAlert(
                        title: "Setting not saved",
                        message: "Controlla ~/.config/meetrec/config.json."
                    )
                    self.autoWanted = self.status.automatic
                }
                self.refreshAll()
            }
        }
    }

    // MARK: Window

    func show() {
        installEditMenuIfNeeded()
        NSApp.activate(ignoringOtherApps: true)
        if !window.isVisible { window.center() }
        window.makeKeyAndOrderFront(nil)
        reloadStatus()
    }

    func windowWillClose(_ notification: Notification) {
        onClose?()
    }
}
