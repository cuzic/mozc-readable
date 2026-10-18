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

#include <cstdint>

namespace mozc {
namespace win32 {
namespace tsf {

inline constexpr uint32_t kTipSharedStateMagic = 0x5A52444D;  // 'MDRZ'
inline constexpr uint32_t kTipSharedStateVersion = 2;

// Layout is part of the external interface. Append only, bump the version on
// incompatible changes. All fields are little-endian 32/64-bit integers.
// Version 2: the former `reserved` field is `active`. Readers must ignore blocks
// whose version is lower than 2 (they cannot tell a deactivated TIP from a live one),
// and must treat seq == 0 as "not published yet" (`magic` is written last).
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
};
static_assert(sizeof(TipSharedState) == 48, "TipSharedState layout changed");

class TipStatePublisher {
 public:
  TipStatePublisher() = delete;

  // Publishes the state for the calling thread. Never blocks and never fails
  // visibly: if the objects cannot be created, this is a no-op.
  static void Publish(bool effective_open, int effective_mode, bool tsf_open,
                      int tsf_mode);

  // Marks the TIP as activated / deactivated on the calling thread
  // (TipTextService::ActivateEx / Deactivate).
  static void SetActive(bool active);
};

}  // namespace tsf
}  // namespace win32
}  // namespace mozc

#endif  // MOZC_WIN32_TIP_TIP_STATE_PUBLISHER_H_
