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

#include "win32/tip/tip_state_publisher.h"

#include <windows.h>

#include <cstdint>
#include <string>

#include "base/win32/win_sandbox.h"

namespace mozc {
namespace win32 {
namespace tsf {
namespace {

constexpr wchar_t kEventName[] = L"Local\\mozc-readable-ime-changed";
constexpr wchar_t kMappingPrefix[] = L"Local\\mozc-readable-ime-";

HANDLE GetChangeEvent() {
  // The handle is intentionally kept for the lifetime of the process.
  static const HANDLE event = [] {
    auto sd = WinSandbox::MakeSecurityDescriptor(WinSandbox::kSharableEvent);
    SECURITY_ATTRIBUTES sa = {
        .nLength = sizeof(SECURITY_ATTRIBUTES),
        .lpSecurityDescriptor = sd.get(),
        .bInheritHandle = FALSE,
    };
    return ::CreateEventW(sd ? &sa : nullptr, /*bManualReset=*/TRUE,
                          /*bInitialState=*/FALSE, kEventName);
  }();
  return event;
}

// Everything that is published, compared as a unit so that "nothing changed" is exact.
struct Snapshot {
  uint32_t effective_open = 0;
  uint32_t effective_mode = 0;
  uint32_t tsf_open = 0;
  uint32_t tsf_mode = 0;
  uint32_t active = 0;
  bool operator==(const Snapshot& o) const {
    return effective_open == o.effective_open &&
           effective_mode == o.effective_mode && tsf_open == o.tsf_open &&
           tsf_mode == o.tsf_mode && active == o.active;
  }
};

// Per-thread mapping. TSF threads are long-lived; the view is released when
// the thread exits (or the DLL is unloaded).
class ThreadSlot {
 public:
  ThreadSlot() = default;
  ThreadSlot(const ThreadSlot&) = delete;
  ThreadSlot& operator=(const ThreadSlot&) = delete;
  ~ThreadSlot() {
    if (state_ != nullptr) ::UnmapViewOfFile(state_);
    if (mapping_ != nullptr) ::CloseHandle(mapping_);
  }

  Snapshot& pending() { return pending_; }

  // Writes `pending_` to the shared block unless it already holds exactly that.
  // Returns true when the block was updated (the caller then pulses the event).
  bool Flush() {
    TipSharedState* s = Get();
    if (s == nullptr) return false;
    if (published_ && last_ == pending_) return false;
    volatile LONG* seq = reinterpret_cast<volatile LONG*>(&s->seq);
    ::InterlockedIncrement(seq);  // odd: writing (full barrier)
    s->effective_open = pending_.effective_open;
    s->effective_mode = pending_.effective_mode;
    s->tsf_open = pending_.tsf_open;
    s->tsf_mode = pending_.tsf_mode;
    s->active = pending_.active;
    s->update_tick_ms = ::GetTickCount64();
    ::InterlockedIncrement(seq);  // even: stable (full barrier)
    last_ = pending_;
    published_ = true;
    return true;
  }

 private:
  TipSharedState* Get() {
    if (state_ != nullptr || tried_) return state_;
    tried_ = true;
    const DWORD tid = ::GetCurrentThreadId();
    const std::wstring name = std::wstring(kMappingPrefix) + std::to_wstring(tid);
    auto sd =
        WinSandbox::MakeSecurityDescriptor(WinSandbox::kSharableFileForRead);
    SECURITY_ATTRIBUTES sa = {
        .nLength = sizeof(SECURITY_ATTRIBUTES),
        .lpSecurityDescriptor = sd.get(),
        .bInheritHandle = FALSE,
    };
    mapping_ = ::CreateFileMappingW(INVALID_HANDLE_VALUE, sd ? &sa : nullptr,
                                    PAGE_READWRITE, 0, sizeof(TipSharedState),
                                    name.c_str());
    if (mapping_ == nullptr) return nullptr;
    void* view =
        ::MapViewOfFile(mapping_, FILE_MAP_WRITE, 0, 0, sizeof(TipSharedState));
    if (view == nullptr) {
      ::CloseHandle(mapping_);
      mapping_ = nullptr;
      return nullptr;
    }
    state_ = static_cast<TipSharedState*>(view);
    // The block is zero-filled. seq == 0 means "not published yet"; `magic` is
    // written last so a reader that checks it never sees a half-initialized header.
    state_->version = kTipSharedStateVersion;
    state_->thread_id = tid;
    state_->process_id = ::GetCurrentProcessId();
    ::MemoryBarrier();
    state_->magic = kTipSharedStateMagic;
    return state_;
  }

  HANDLE mapping_ = nullptr;
  TipSharedState* state_ = nullptr;
  bool tried_ = false;
  bool published_ = false;
  Snapshot pending_;
  Snapshot last_;
};

ThreadSlot& CurrentSlot() {
  thread_local ThreadSlot slot;
  return slot;
}

void Pulse() {
  if (HANDLE ev = GetChangeEvent(); ev != nullptr) {
    ::SetEvent(ev);
    ::ResetEvent(ev);
  }
}

}  // namespace

void TipStatePublisher::Publish(bool effective_open, int effective_mode,
                                bool tsf_open, int tsf_mode) {
  ThreadSlot& slot = CurrentSlot();
  Snapshot& p = slot.pending();
  p.effective_open = effective_open ? 1 : 0;
  p.effective_mode = static_cast<uint32_t>(effective_mode);
  p.tsf_open = tsf_open ? 1 : 0;
  p.tsf_mode = static_cast<uint32_t>(tsf_mode);
  // Every mutator of TipInputModeManager calls in here, mostly without any
  // change (focus moves, mouse clicks): Flush() does nothing for those.
  if (slot.Flush()) Pulse();
}

void TipStatePublisher::SetActive(bool active) {
  ThreadSlot& slot = CurrentSlot();
  slot.pending().active = active ? 1 : 0;
  if (slot.Flush()) Pulse();
}

}  // namespace tsf
}  // namespace win32
}  // namespace mozc
