// Copyright 2010-2021, Google Inc.
// All rights reserved.
//
// Redistribution and use in source and binary forms, with or without
// modification, are permitted provided that the following conditions are
// met:
//
//     * Redistributions of source code must retain the above copyright
// notice, this list of conditions and the following disclaimer.
//     * Redistributions in binary form must reproduce the above
// copyright notice, this list of conditions and the following disclaimer
// in the documentation and/or other materials provided with the
// distribution.
//     * Neither the name of Google Inc. nor the names of its
// contributors may be used to endorse or promote products derived from
// this software without specific prior written permission.
//
// THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
// "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
// LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
// A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
// OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
// SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
// LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
// DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
// THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
// (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
// OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

// current TSF thread to a named shared-memory block so that other processes
// can read it without any IPC handshake.
//
// Reading protocol (seqlock): read `seq` (must be even), read the payload,
// read `seq` again; accept the payload only if both reads are equal.
// Object names (Local\ namespace of the interactive session):
//   mapping: Local\mozc-readable-ime-<thread id>   (TipSharedState)
//   event:   Local\mozc-readable-ime-changed       (manual-reset; every publish
//            pulses it with SetEvent immediately followed by ResetEvent, so
//            all waiters are released. A waiter that is busy while a pulse
//            happens misses it and must re-read the state it cares about
//            after every wake-up.)

#ifndef MOZC_WIN32_TIP_TIP_STATE_PUBLISHER_H_
#define MOZC_WIN32_TIP_TIP_STATE_PUBLISHER_H_

#include <cstddef>
#include <cstdint>

namespace mozc {
namespace win32 {
namespace tsf {

inline constexpr uint32_t kTipSharedStateMagic = 0x5A52444D;  // 'MDRZ'
inline constexpr uint32_t kTipSharedStateVersion = 6;
// Size of the version 2 layout. A block of this size left by an older DLL is
// still written (version 2, first 48 bytes only).
inline constexpr size_t kTipSharedStateV2Size = 48;
// Size of the version 3 layout (ADR-267). A block of this size left by an older
// DLL is still written (version 3, first 80 bytes only).
inline constexpr size_t kTipSharedStateV3Size = 80;
// Size of the version 4 layout. A block of this size left by an older DLL is
// still written (version 4, first 112 bytes only). This is the layout of the
// latest actually-released build (v3.35.6354-readable.1) -- offsets 0..111 are
// frozen and must never be reordered or reused.
inline constexpr size_t kTipSharedStateV4Size = 112;
// Size of the version 5 layout. A block of this size left by an older DLL is
// still written (version 5, first 160 bytes only). No released build has ever
// shipped version 5 yet as of this writing -- see the version 6 comment below --
// but the fallback chain is kept symmetric with v2/v3/v4 in case one does before
// v6 ships.
inline constexpr size_t kTipSharedStateV5Size = 160;

// Layout is part of the external interface. Append only, bump the version on
// incompatible changes. All fields are little-endian 32/64-bit integers.
// Version 2: the former `reserved` field is `active`. Readers must ignore blocks
// whose version is lower than 2 (they cannot tell a deactivated TIP from a live one),
// and must treat seq == 0 as "not published yet" (`magic` is written last).
// Version 3 (ADR-267): 32 bytes appended (offsets 48..79). Offsets 0..47 are
// unchanged. Readers must ignore every version 3 field while active == 0.
// Version 4 (diagnostics for "composition is committed on every key", Windows
// Terminal): 32 bytes appended (offsets 80..111). Offsets 0..79 are unchanged.
// Counters are monotonic (wrap), written when they change and never pulsed.
//
// Version 5 (offsets 112..159, 48 bytes appended; offsets 0..111 unchanged):
// what had briefly been three separate versions (5/6/7) on an unreleased branch
// got consolidated into this single version before its first release, grouped
// state-first then diagnostics-only (no released build ever shipped the old
// 5/6/7 layout, so there was nothing to preserve -- see
// readable/docs/shared-memory.md changelog):
// - `prefer_kana_input`/`use_romaji_key_to_toggle_input_style` (offsets
//   112..119): Mozc's own input style (romaji vs. JIS kana direct input,
//   *not* an OS keyboard lock bit -- see awase's ADR-287). `prefer_kana_input`
//   is the raw value of the focused edit context's
//   InputBehavior::prefer_kana_input; it can flip without any config dialog
//   change, see `use_romaji_key_to_toggle_input_style`/VK_DBE_ROMAN/
//   VK_DBE_NOROMAN in win32/base/keyevent_handler.cc. Treated as state (like
//   effective_mode): written and pulses the change event when either value
//   changes.
// - keyboard-focus timing (offsets 120..151, for "the first keys after a focus
//   change are not seen by the TIP"): `key_sink_focus_tick_ms`,
//   `key_sink_blur_tick_ms`, `thread_focus_tick_ms`, `first_key_tick_ms`. The
//   ticks are GetTickCount64() values (0 = never). Diagnostics-only: written
//   when they change, never pulsed.
// - `key_eaten_count`/`key_passed_count` (offsets 152..159, diagnostics-only,
//   never pulsed): key-downs the key sink ate vs. passed to the app.
//
// Version 6 (offsets 160..175, 16 bytes appended; offsets 0..159 unchanged,
// EXPERIMENTAL -- not released, see awase's exp/direct-actuation-channel):
// a direct-actuation request/ack channel, so a caller (awase) can ask this
// TIP to turn the IME on/off without synthesizing a keystroke and inferring
// success by polling. The request itself travels over a registered window
// message (see tip_text_service.cc, kDirectActuationMessageName), not through
// this shared memory; this struct only carries the *result* of the last
// request this thread processed, the same way `server_ready` already reports
// the outcome of a key-triggered server round trip:
// - `last_cmd_nonce`/`last_cmd_result` (state, pulses): the nonce the caller
//   passed in the request's WPARAM, and what happened to it (see
//   TipDirectActuationResult). 0/kNone until the first request is processed.
//   Written (and the change event pulsed) from the window-message handler,
//   after the normal key-triggered Publish() for any state change the
//   request itself caused has already happened -- so a reader that is
//   waiting for `last_cmd_nonce == <the nonce it sent>` always also sees the
//   up-to-date effective_open/tsf_open by the time it wakes.
// - `cmd_window_handle` (diagnostics-only, never pulsed): the HWND of this
//   thread's direct-actuation window, so the caller does not need to
//   enumerate windows to find it. 0 before the window exists (not yet
//   activated) or if window creation failed.
// See readable/docs/shared-memory.md for the meaning of each field.
struct TipSharedState {
  uint32_t magic;           // kTipSharedStateMagic
  uint32_t version;         // kTipSharedStateVersion
  uint32_t seq;             // odd while being written
  uint32_t effective_open;  // 0/1: IME open state seen by the Mozc session
  uint32_t effective_mode;  // commands::CompositionMode (Mozc session)
  uint32_t tsf_open;        // 0/1: IME open state visible to TSF
  uint32_t tsf_mode;        // commands::CompositionMode (TSF)
  uint32_t thread_id;
  uint32_t process_id;
  uint32_t active;          // 1 while this TIP is activated on the thread (Activate..Deactivate)
  uint64_t update_tick_ms;  // GetTickCount64() of the last publish
  // ---- version 3 ----
  uint32_t phase;                 // 0 none, 1 composing, 2 converting
  uint32_t candidate_category;    // 0 none, 1 SUGGESTION, 2 PREDICTION, 3 CONVERSION
  uint32_t preedit_utf16_len;     // length of the preedit in UTF-16 code units
  uint32_t preedit_cursor;        // UTF-32 code points (Mozc's unit)
  uint32_t key_seq;               // key-downs seen by OnTestKeyDown/OnPreservedKey (or OnKeyDown alone)
  uint32_t scope_forced_mode;     // 0 none, else commands::CompositionMode + 1
  uint32_t server_ready;          // 1 once a server response was received
  uint32_t composition_key_seq;   // key_seq of the key that produced the composition fields
  // ---- version 4 ----
  uint32_t composition_start_count;  // phase went 0 -> non-zero
  uint32_t submit_end_edit_count;    // SUBMIT requested by OnEndEdit (selection left the composition)
  uint32_t submit_terminated_count;  // SUBMIT sent by OnCompositionTerminated (the app ended it)
  uint32_t revert_count;             // REVERT requested by OnEndEdit (composition range empty)
  uint32_t submit_other_count;       // SUBMIT requested by a UI element (candidate window / indicator)
  uint32_t edit_session_fail_count;  // RequestEditSession failed (or its session returned a failure)
  uint32_t diag_flags;               // bit0: use_async_lock_in_key_handler, bit1: an Update saw last_output_is_test (sticky)
  uint32_t update_context_id;        // serial number of the TipPrivateContext TipUiHandler::Update last read (not an address)
  // ---- version 5 (state first, then diagnostics-only; see comment above) ----
  uint32_t prefer_kana_input;                      // 0/1: InputBehavior::prefer_kana_input of the focused edit context (0=romaji, 1=JIS kana direct input)
  uint32_t use_romaji_key_to_toggle_input_style;    // 0/1: config use_keyboard_to_change_preedit_method (VK_DBE_ROMAN/NOROMAN flips prefer_kana_input when 1)
  uint64_t key_sink_focus_tick_ms;   // ITfKeyEventSink::OnSetFocus(TRUE): this TIP became the keyboard-focused key sink
  uint64_t key_sink_blur_tick_ms;    // ITfKeyEventSink::OnSetFocus(FALSE)
  uint64_t thread_focus_tick_ms;     // ITfThreadFocusSink::OnSetThreadFocus (OS keyboard focus reached the thread)
  uint64_t first_key_tick_ms;        // the first key-down counted after key_sink_focus_tick_ms (0 until one arrives)
  uint32_t key_eaten_count;          // key-downs counted in key_seq that the key sink ate (eaten = TRUE)
  uint32_t key_passed_count;         // key-downs counted in key_seq that the key sink did not eat (passed to the app)
  // ---- version 6 (EXPERIMENTAL, unreleased) ----
  uint32_t last_cmd_nonce;    // nonce of the last direct-actuation request this thread processed (0 = none yet)
  uint32_t last_cmd_result;   // TipDirectActuationResult for last_cmd_nonce
  uint64_t cmd_window_handle; // HWND (as uint64_t) of this thread's direct-actuation window, 0 if none
};
static_assert(sizeof(TipSharedState) == 176, "TipSharedState layout changed");
static_assert(offsetof(TipSharedState, last_cmd_nonce) == kTipSharedStateV5Size,
              "TipSharedState layout changed");
static_assert(offsetof(TipSharedState, cmd_window_handle) == 168,
              "TipSharedState layout changed");
static_assert(offsetof(TipSharedState, prefer_kana_input) == kTipSharedStateV4Size,
              "TipSharedState layout changed");
static_assert(offsetof(TipSharedState, key_sink_focus_tick_ms) == 120,
              "TipSharedState layout changed");
static_assert(offsetof(TipSharedState, key_eaten_count) == 152,
              "TipSharedState layout changed");
static_assert(offsetof(TipSharedState, first_key_tick_ms) == 144,
              "TipSharedState layout changed");
static_assert(offsetof(TipSharedState, composition_key_seq) + sizeof(uint32_t) ==
                  kTipSharedStateV3Size,
              "TipSharedState layout changed");
static_assert(offsetof(TipSharedState, composition_start_count) == 80,
              "TipSharedState layout changed");
static_assert(offsetof(TipSharedState, edit_session_fail_count) == 100,
              "TipSharedState layout changed");
static_assert(offsetof(TipSharedState, update_context_id) == 108,
              "TipSharedState layout changed");
static_assert(offsetof(TipSharedState, update_tick_ms) == 40,
              "TipSharedState layout changed");
static_assert(offsetof(TipSharedState, phase) == kTipSharedStateV2Size,
              "TipSharedState layout changed");
static_assert(offsetof(TipSharedState, key_seq) == 64,
              "TipSharedState layout changed");
static_assert(offsetof(TipSharedState, composition_key_seq) == 76,
              "TipSharedState layout changed");

// The composition fields, produced from `TipPrivateContext::last_output()` in
// TipUiHandler::Update (ADR-267 D7).
struct TipCompositionState {
  uint32_t phase = 0;
  uint32_t candidate_category = 0;
  uint32_t preedit_utf16_len = 0;
  uint32_t preedit_cursor = 0;
  uint32_t composition_key_seq = 0;
};

// What requested a SUBMIT / REVERT (the counters of version 4).
enum class TipSubmitCause {
  kEndEdit,     // OnEndEdit: the selection left the composition range
  kTerminated,  // ITfCompositionSink::OnCompositionTerminated
  kRevert,      // OnEndEdit: the composition range became empty
  kOther,       // a candidate window / indicator UI element
};

// The outcome of one direct-actuation request (version 6, EXPERIMENTAL). Numeric
// values are part of the wire contract with awase and must not be renumbered.
enum class TipDirectActuationResult : uint32_t {
  kNone = 0,               // no request processed yet since Activate
  kApplied = 1,            // SendCommand succeeded and the edit session ran
  kNoFocusedContext = 2,   // this thread has no focused ITfContext right now
  kSendCommandFailed = 3,  // the mozc session client rejected the command
  kReservedBitsSet = 4,    // the request's reserved LPARAM bits were nonzero
};

class TipStatePublisher {
 public:
  TipStatePublisher() = delete;

  // Publishes the state for the calling thread. Never blocks and never fails
  // visibly: if the objects cannot be created, this is a no-op.
  // `scope_forced_mode` is 0 (no input scope override) or the
  // commands::CompositionMode the input scope forces, plus 1.
  static void Publish(bool effective_open, int effective_mode, bool tsf_open,
                      int tsf_mode, uint32_t scope_forced_mode);

  // Marks the TIP as activated / deactivated on the calling thread
  // (TipTextService::ActivateEx / Deactivate).
  // Deactivating also zeroes the composition fields and scope_forced_mode
  // (key_seq is kept).
  static void SetActive(bool active);

  // Publishes the composition fields. Writes whenever any field changed, but
  // pulses the change event only when `phase` or `candidate_category` changed.
  static void SetComposition(const TipCompositionState& composition);

  // Counts one key-down seen by the TIP (key_seq += 1). Written on every call,
  // never pulses.
  // `vk` is remembered as the key that was counted; kAnyKey (a preserved key,
  // whose VK is known only later) matches every key.
  static constexpr uint32_t kAnyKey = 0xFFFFFFFFu;
  static void CountKeyDown(uint32_t vk = kAnyKey);

  // Called from OnKeyDown. If this very key-down was already counted by
  // CountKeyDown() (OnTestKeyDown ate it, or OnPreservedKey), consumes that
  // mark and counts nothing. (TipTextService::OnPreservedKey calls
  // TipKeyeventHandler::OnKeyDown itself, which is why its mark is consumed;
  // TSF does not call OnKeyDown after OnPreservedKey. Keep that call if you
  // change OnPreservedKey, or forget the mark with ForgetCountedKeyDown().)
  // Otherwise the host called OnKeyDown without a
  // preceding OnTestKeyDown for it (observed in Windows Terminal), so counts it.
  // Returns true if the key-down had already been counted (its result was noted
  // by the caller that counted it).
  static bool CountKeyDownUnlessCounted(uint32_t vk);

  // Forgets the newest "already counted" mark: the key CountKeyDown() just
  // counted will not reach OnKeyDown (OnTestKeyDown did not eat it, or
  // OnPreservedKey found no such preserved key).
  static void ForgetCountedKeyDown();

  // Version 5: the outcome of one counted key-down (eaten by the key sink, or
  // passed to the application). Exactly one call per key_seq increment, so that
  // key_eaten_count + key_passed_count == key_seq for the keys counted since
  // the TIP loaded. The outcome is the one OnTestKeyDown returned, or OnKeyDown's
  // for a host that skipped OnTestKeyDown. Written when it changes, never pulsed.
  static void NoteKeyResult(bool eaten);

  // The current key_seq of the calling thread.
  static uint32_t CurrentKeySeq();

  // Version 4 counters. Each writes the block when it changes and never pulses.
  static void CountSubmit(TipSubmitCause cause);
  static void CountEditSessionFailure();
  // The use_async_lock_in_key_handler flag of the thread context.
  static void SetAsyncLockInKeyHandler(bool value);
  // What TipUiHandler::Update read: the context's serial number and
  // whether its last_output() was a TEST_SEND_KEY response (sticky bit).
  static void SetUpdateInfo(uint32_t context_id, bool last_output_is_test);

  // Records whether the last call to the Mozc server got a response.
  static void SetServerReady(bool ready);

  // Version 5 keyboard-focus timing. Each writes the block when it changes and
  // never pulses.
  // ITfKeyEventSink::OnSetFocus. A `foreground` focus also restarts the wait for
  // the first key-down (first_key_tick_ms is cleared).
  static void NoteKeySinkFocus(bool foreground);
  // ITfThreadFocusSink::OnSetThreadFocus.
  static void NoteThreadFocus();

  // Version 5: the focused edit context's input style (InputBehavior::
  // prefer_kana_input) and whether VK_DBE_ROMAN/VK_DBE_NOROMAN can flip it
  // (use_keyboard_to_change_preedit_method). Called both when the config is
  // first read (TipPrivateContext::EnsureInitialized) and whenever the key
  // handler flips prefer_kana_input (win32/base/keyevent_handler.cc). Written
  // when either value changes; pulses the change event.
  static void SetInputStyle(bool prefer_kana_input, bool toggle_enabled);

  // Version 6 (EXPERIMENTAL): the HWND (as uint64_t; the caller casts to/from
  // HWND) of this thread's direct-actuation window. Pass 0 when the window is
  // destroyed (Deactivate) or could not be created. Diagnostics-only, never
  // pulses: a reader that wants the window handle already has to read the
  // block anyway to get thread_id/process_id to send to it.
  static void PublishCommandWindow(uint64_t window_handle);

  // Version 6 (EXPERIMENTAL): records the outcome of the direct-actuation
  // request `nonce`. Called from the window-message handler after any
  // resulting Publish() from the normal key-triggered path has already run,
  // so a reader that wakes on `last_cmd_nonce == nonce` also sees the
  // up-to-date effective_open/tsf_open. Always pulses (state field).
  static void SetLastCommandResult(uint32_t nonce, TipDirectActuationResult result);
};

}  // namespace tsf
}  // namespace win32
}  // namespace mozc

#endif  // MOZC_WIN32_TIP_TIP_STATE_PUBLISHER_H_
