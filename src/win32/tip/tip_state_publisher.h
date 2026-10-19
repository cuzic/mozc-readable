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
inline constexpr uint32_t kTipSharedStateVersion = 3;
// Size of the version 2 layout. A block of this size left by an older DLL is
// still written (version 2, first 48 bytes only).
inline constexpr size_t kTipSharedStateV2Size = 48;

// Layout is part of the external interface. Append only, bump the version on
// incompatible changes. All fields are little-endian 32/64-bit integers.
// Version 2: the former `reserved` field is `active`. Readers must ignore blocks
// whose version is lower than 2 (they cannot tell a deactivated TIP from a live one),
// and must treat seq == 0 as "not published yet" (`magic` is written last).
// Version 3 (ADR-267): 32 bytes appended (offsets 48..79). Offsets 0..47 are
// unchanged. Readers must ignore every version 3 field while active == 0.
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
};
static_assert(sizeof(TipSharedState) == 80, "TipSharedState layout changed");
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
  static void CountKeyDownUnlessCounted(uint32_t vk);

  // Forgets the "already counted" mark: OnTestKeyDown did not eat the key, so
  // TSF will not call OnKeyDown for it.
  static void ForgetCountedKeyDown();

  // The current key_seq of the calling thread.
  static uint32_t CurrentKeySeq();

  // Records whether the last call to the Mozc server got a response.
  static void SetServerReady(bool ready);
};

}  // namespace tsf
}  // namespace win32
}  // namespace mozc

#endif  // MOZC_WIN32_TIP_TIP_STATE_PUBLISHER_H_
