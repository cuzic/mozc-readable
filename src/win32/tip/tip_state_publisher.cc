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

#include <cstddef>
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
  uint32_t phase = 0;
  uint32_t candidate_category = 0;
  uint32_t preedit_utf16_len = 0;
  uint32_t preedit_cursor = 0;
  uint32_t key_seq = 0;
  uint32_t scope_forced_mode = 0;
  uint32_t server_ready = 0;
  uint32_t composition_key_seq = 0;
  // Version 4 (never part of SameState: they do not pulse).
  uint32_t composition_start_count = 0;
  uint32_t submit_end_edit_count = 0;
  uint32_t submit_terminated_count = 0;
  uint32_t revert_count = 0;
  uint32_t submit_other_count = 0;
  uint32_t edit_session_fail_count = 0;
  uint32_t diag_flags = 0;
  uint32_t update_context_id = 0;
  // "Is there anything to write": every published field.
  bool operator==(const Snapshot& o) const {
    return SameState(o) && preedit_utf16_len == o.preedit_utf16_len &&
           preedit_cursor == o.preedit_cursor && key_seq == o.key_seq &&
           composition_key_seq == o.composition_key_seq &&
           composition_start_count == o.composition_start_count &&
           submit_end_edit_count == o.submit_end_edit_count &&
           submit_terminated_count == o.submit_terminated_count &&
           revert_count == o.revert_count &&
           submit_other_count == o.submit_other_count &&
           edit_session_fail_count == o.edit_session_fail_count &&
           diag_flags == o.diag_flags && update_context_id == o.update_context_id;
  }
  // "Should the change event be pulsed": the state fields only. Changes of
  // preedit_utf16_len, preedit_cursor, key_seq and composition_key_seq alone
  // are written without waking the waiters (ADR-267 D6).
  bool SameState(const Snapshot& o) const {
    return effective_open == o.effective_open &&
           effective_mode == o.effective_mode && tsf_open == o.tsf_open &&
           tsf_mode == o.tsf_mode && active == o.active && phase == o.phase &&
           candidate_category == o.candidate_category &&
           scope_forced_mode == o.scope_forced_mode &&
           server_ready == o.server_ready;
  }
};

struct FlushResult {
  bool wrote = false;
  bool pulse = false;
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

  bool published() const { return published_; }

  // Writes `pending_` to the shared block unless it already holds exactly that
  // (`wrote`). The caller pulses the event only when `pulse` is set: the first
  // write, or a change of the state fields (the write and the pulse are decided
  // separately, ADR-267 D6).
  FlushResult Flush() {
    TipSharedState* s = Get();
    if (s == nullptr) return {};
    if (published_ && last_ == pending_) return {};
    const bool pulse = !published_ || !last_.SameState(pending_);
    volatile LONG* seq = reinterpret_cast<volatile LONG*>(&s->seq);
    ::InterlockedIncrement(seq);  // odd: writing (full barrier)
    s->effective_open = pending_.effective_open;
    s->effective_mode = pending_.effective_mode;
    s->tsf_open = pending_.tsf_open;
    s->tsf_mode = pending_.tsf_mode;
    s->active = pending_.active;
    s->update_tick_ms = ::GetTickCount64();
    if (size_ >= kTipSharedStateV3Size) {
      // An older, 48 byte block is never touched beyond its end (ADR-267 D9).
      s->phase = pending_.phase;
      s->candidate_category = pending_.candidate_category;
      s->preedit_utf16_len = pending_.preedit_utf16_len;
      s->preedit_cursor = pending_.preedit_cursor;
      s->key_seq = pending_.key_seq;
      s->scope_forced_mode = pending_.scope_forced_mode;
      s->server_ready = pending_.server_ready;
      s->composition_key_seq = pending_.composition_key_seq;
    }
    if (size_ >= sizeof(TipSharedState)) {
      // Likewise an 80 byte (version 3) block is never touched beyond its end.
      s->composition_start_count = pending_.composition_start_count;
      s->submit_end_edit_count = pending_.submit_end_edit_count;
      s->submit_terminated_count = pending_.submit_terminated_count;
      s->revert_count = pending_.revert_count;
      s->submit_other_count = pending_.submit_other_count;
      s->edit_session_fail_count = pending_.edit_session_fail_count;
      s->diag_flags = pending_.diag_flags;
      s->update_context_id = pending_.update_context_id;
    }
    ::InterlockedIncrement(seq);  // even: stable (full barrier)
    last_ = pending_;
    published_ = true;
    return {true, pulse};
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
    const bool existed = ::GetLastError() == ERROR_ALREADY_EXISTS;
    size_ = sizeof(TipSharedState);
    void* view = ::MapViewOfFile(mapping_, FILE_MAP_WRITE, 0, 0, size_);
    if (view == nullptr && existed) {
      // A section left by an older DLL has the version 3 size (80), or the
      // version 2 size (48). Write that version into it (ADR-267 D9).
      size_ = kTipSharedStateV3Size;
      view = ::MapViewOfFile(mapping_, FILE_MAP_WRITE, 0, 0, size_);
      if (view == nullptr) {
        size_ = kTipSharedStateV2Size;
        view = ::MapViewOfFile(mapping_, FILE_MAP_WRITE, 0, 0, size_);
      }
    }
    if (view == nullptr) {
      ::CloseHandle(mapping_);
      mapping_ = nullptr;
      size_ = 0;
      return nullptr;
    }
    state_ = static_cast<TipSharedState*>(view);
    // The block is zero-filled. seq == 0 means "not published yet"; `magic` is
    // written last so a reader that checks it never sees a half-initialized header.
    // `Get()` writes the version unconditionally, so a 48 byte block must be
    // labelled 2 and an 80 byte block 3, or a reader would map more than there is.
    state_->version = size_ >= sizeof(TipSharedState) ? kTipSharedStateVersion
                      : size_ >= kTipSharedStateV3Size ? 3
                                                       : 2;
    state_->thread_id = tid;
    state_->process_id = ::GetCurrentProcessId();
    ::MemoryBarrier();
    state_->magic = kTipSharedStateMagic;
    return state_;
  }

  HANDLE mapping_ = nullptr;
  TipSharedState* state_ = nullptr;
  size_t size_ = 0;  // mapped (and writable) size: 112, or 80 / 48 for an old block
  bool tried_ = false;
  bool published_ = false;
  Snapshot pending_;
  Snapshot last_;
};

ThreadSlot& CurrentSlot() {
  thread_local ThreadSlot slot;
  return slot;
}

// The VK of the key-down CountKeyDown() counted (kNotCounted: none), consumed by
// the OnKeyDown that follows the counted OnTestKeyDown/OnPreservedKey. A mark
// that is never consumed (TSF did not call OnKeyDown) can then only swallow the
// count of a later key-down of the same key. Per thread, like the slot.
constexpr uint32_t kNotCounted = 0xFFFFFFFEu;
uint32_t& CountedVk() {
  thread_local uint32_t vk = kNotCounted;
  return vk;
}

void Pulse() {
  if (HANDLE ev = GetChangeEvent(); ev != nullptr) {
    ::SetEvent(ev);
    ::ResetEvent(ev);
  }
}

}  // namespace

void TipStatePublisher::Publish(bool effective_open, int effective_mode,
                                bool tsf_open, int tsf_mode,
                                uint32_t scope_forced_mode) {
  ThreadSlot& slot = CurrentSlot();
  Snapshot& p = slot.pending();
  p.effective_open = effective_open ? 1 : 0;
  p.effective_mode = static_cast<uint32_t>(effective_mode);
  p.tsf_open = tsf_open ? 1 : 0;
  p.tsf_mode = static_cast<uint32_t>(tsf_mode);
  p.scope_forced_mode = scope_forced_mode;
  // Every mutator of TipInputModeManager calls in here, mostly without any
  // change (focus moves, mouse clicks): Flush() does nothing for those.
  if (slot.Flush().pulse) Pulse();
}

void TipStatePublisher::SetActive(bool active) {
  ThreadSlot& slot = CurrentSlot();
  Snapshot& p = slot.pending();
  p.active = active ? 1 : 0;
  if (!active) {
    // The composition fields would otherwise outlive the TIP (ADR-267 D9).
    // key_seq is a counter and stays.
    p.phase = 0;
    p.candidate_category = 0;
    p.preedit_utf16_len = 0;
    p.preedit_cursor = 0;
    p.scope_forced_mode = 0;
    p.composition_key_seq = 0;
  }
  if (slot.Flush().pulse) Pulse();
}

void TipStatePublisher::SetComposition(const TipCompositionState& composition) {
  ThreadSlot& slot = CurrentSlot();
  Snapshot& p = slot.pending();
  if (p.phase == 0 && composition.phase != 0) ++p.composition_start_count;
  p.phase = composition.phase;
  p.candidate_category = composition.candidate_category;
  p.preedit_utf16_len = composition.preedit_utf16_len;
  p.preedit_cursor = composition.preedit_cursor;
  p.composition_key_seq = composition.composition_key_seq;
  // Before the first Publish() the block holds nothing the readers could
  // trust (active is still 0); leave it to that first write.
  if (slot.published() && slot.Flush().pulse) Pulse();
}

void TipStatePublisher::CountKeyDownUnlessCounted(uint32_t vk) {
  uint32_t& counted = CountedVk();
  const bool already = counted == kAnyKey || counted == vk;
  counted = kNotCounted;
  if (!already) CountKeyDown(vk);
  counted = kNotCounted;
}

void TipStatePublisher::ForgetCountedKeyDown() { CountedVk() = kNotCounted; }

void TipStatePublisher::CountKeyDown(uint32_t vk) {
  CountedVk() = vk;
  ThreadSlot& slot = CurrentSlot();
  ++slot.pending().key_seq;
  if (slot.published() && slot.Flush().pulse) Pulse();
}

uint32_t TipStatePublisher::CurrentKeySeq() {
  return CurrentSlot().pending().key_seq;
}

namespace {
// Writes a version 4 diagnostic that changed. Never pulses (ADR-267 D6); before
// the first Publish() the value waits in `pending_` for that first write.
void FlushDiagnostic(ThreadSlot& slot) {
  if (slot.published()) slot.Flush();
}
}  // namespace

void TipStatePublisher::CountSubmit(TipSubmitCause cause) {
  ThreadSlot& slot = CurrentSlot();
  Snapshot& p = slot.pending();
  switch (cause) {
    case TipSubmitCause::kEndEdit:
      ++p.submit_end_edit_count;
      break;
    case TipSubmitCause::kTerminated:
      ++p.submit_terminated_count;
      break;
    case TipSubmitCause::kRevert:
      ++p.revert_count;
      break;
    case TipSubmitCause::kOther:
      ++p.submit_other_count;
      break;
  }
  FlushDiagnostic(slot);
}

void TipStatePublisher::CountEditSessionFailure() {
  ThreadSlot& slot = CurrentSlot();
  ++slot.pending().edit_session_fail_count;
  FlushDiagnostic(slot);
}

void TipStatePublisher::SetAsyncLockInKeyHandler(bool value) {
  ThreadSlot& slot = CurrentSlot();
  uint32_t& flags = slot.pending().diag_flags;
  flags = value ? (flags | 1u) : (flags & ~1u);
  FlushDiagnostic(slot);
}

void TipStatePublisher::SetUpdateInfo(uint32_t context_id,
                                      bool last_output_is_test) {
  ThreadSlot& slot = CurrentSlot();
  Snapshot& p = slot.pending();
  p.update_context_id = context_id;
  p.diag_flags = last_output_is_test ? (p.diag_flags | 2u) : (p.diag_flags & ~2u);
  FlushDiagnostic(slot);
}

void TipStatePublisher::SetServerReady(bool ready) {
  ThreadSlot& slot = CurrentSlot();
  slot.pending().server_ready = ready ? 1 : 0;
  if (slot.published() && slot.Flush().pulse) Pulse();
}

}  // namespace tsf
}  // namespace win32
}  // namespace mozc
