/*
 * kernel_rtl.c - Xbox Runtime Library Functions
 *
 * Implements Rtl* functions: critical sections, string init/conversion,
 * NTSTATUS→Win32 error mapping, time conversion, sprintf variants.
 *
 * Most of these map 1:1 to Win32 CRT functions.
 */

#include "kernel.h"
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <ctype.h>

/* ============================================================================
 * String Initialization
 * ============================================================================ */

VOID __stdcall xbox_RtlInitAnsiString(PXBOX_ANSI_STRING DestinationString, const char* SourceString)
{
    if (SourceString) {
        USHORT len = (USHORT)strlen(SourceString);
        DestinationString->Length = len;
        DestinationString->MaximumLength = len + 1;
        DestinationString->Buffer = (PCHAR)SourceString;
    } else {
        DestinationString->Length = 0;
        DestinationString->MaximumLength = 0;
        DestinationString->Buffer = NULL;
    }
}

VOID __stdcall xbox_RtlInitUnicodeString(PXBOX_UNICODE_STRING DestinationString, const WCHAR* SourceString)
{
    if (SourceString) {
        USHORT len = (USHORT)(wcslen(SourceString) * sizeof(WCHAR));
        DestinationString->Length = len;
        DestinationString->MaximumLength = len + sizeof(WCHAR);
        DestinationString->Buffer = (PWCHAR)SourceString;
    } else {
        DestinationString->Length = 0;
        DestinationString->MaximumLength = 0;
        DestinationString->Buffer = NULL;
    }
}

/* ============================================================================
 * String Conversion (ANSI ↔ Unicode)
 * ============================================================================ */

NTSTATUS __stdcall xbox_RtlAnsiStringToUnicodeString(
    PXBOX_UNICODE_STRING DestinationString,
    PXBOX_ANSI_STRING SourceString,
    BOOLEAN AllocateDestinationString)
{
    ULONG unicode_len;

    if (!DestinationString || !SourceString)
        return STATUS_INVALID_PARAMETER;

    unicode_len = (SourceString->Length + 1) * sizeof(WCHAR);

    if (AllocateDestinationString) {
        DestinationString->Buffer = (PWCHAR)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, unicode_len);
        if (!DestinationString->Buffer)
            return STATUS_NO_MEMORY;
        DestinationString->MaximumLength = (USHORT)unicode_len;
    } else if (DestinationString->MaximumLength < unicode_len) {
        return STATUS_BUFFER_OVERFLOW;
    }

    int result = MultiByteToWideChar(CP_ACP, 0,
        SourceString->Buffer, SourceString->Length,
        DestinationString->Buffer, DestinationString->MaximumLength / sizeof(WCHAR));

    if (result > 0) {
        DestinationString->Length = (USHORT)(result * sizeof(WCHAR));
        DestinationString->Buffer[result] = L'\0';
        return STATUS_SUCCESS;
    }

    return STATUS_UNSUCCESSFUL;
}

NTSTATUS __stdcall xbox_RtlUnicodeStringToAnsiString(
    PXBOX_ANSI_STRING DestinationString,
    PXBOX_UNICODE_STRING SourceString,
    BOOLEAN AllocateDestinationString)
{
    ULONG ansi_len;

    if (!DestinationString || !SourceString)
        return STATUS_INVALID_PARAMETER;

    ansi_len = SourceString->Length / sizeof(WCHAR) + 1;

    if (AllocateDestinationString) {
        DestinationString->Buffer = (PCHAR)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, ansi_len);
        if (!DestinationString->Buffer)
            return STATUS_NO_MEMORY;
        DestinationString->MaximumLength = (USHORT)ansi_len;
    } else if (DestinationString->MaximumLength < ansi_len) {
        return STATUS_BUFFER_OVERFLOW;
    }

    int result = WideCharToMultiByte(CP_ACP, 0,
        SourceString->Buffer, SourceString->Length / sizeof(WCHAR),
        DestinationString->Buffer, DestinationString->MaximumLength,
        NULL, NULL);

    if (result >= 0) {
        DestinationString->Length = (USHORT)result;
        if ((USHORT)result < DestinationString->MaximumLength)
            DestinationString->Buffer[result] = '\0';
        return STATUS_SUCCESS;
    }

    return STATUS_UNSUCCESSFUL;
}

/* ============================================================================
 * String Comparison
 * ============================================================================ */

BOOLEAN __stdcall xbox_RtlEqualString(
    PXBOX_ANSI_STRING String1,
    PXBOX_ANSI_STRING String2,
    BOOLEAN CaseInSensitive)
{
    if (String1->Length != String2->Length)
        return FALSE;

    if (CaseInSensitive)
        return _strnicmp(String1->Buffer, String2->Buffer, String1->Length) == 0;
    else
        return strncmp(String1->Buffer, String2->Buffer, String1->Length) == 0;
}

/*
 * RtlCompareMemoryUlong - Scans memory for a ULONG pattern.
 * Returns the number of bytes that matched.
 */
ULONG __stdcall xbox_RtlCompareMemoryUlong(PVOID Source, ULONG Length, ULONG Pattern)
{
    PULONG src = (PULONG)Source;
    ULONG count = Length / sizeof(ULONG);

    for (ULONG i = 0; i < count; i++) {
        if (src[i] != Pattern)
            return i * sizeof(ULONG);
    }
    return count * sizeof(ULONG);
}

/* ============================================================================
 * Critical Sections (direct 1:1 mapping)
 * ============================================================================ */

/*
 * Critical sections: a shadow map from guest critical section -> native lock.
 *
 * These were no-ops, and that was CORRECT while every Xbox thread ran
 * synchronously - one OS thread means no contention to lose. Real threads
 * invert it: these calls are the only thing the game uses to protect its
 * shared structures, so a no-op here is a data race in every one of them.
 *
 * The Xbox RTL_CRITICAL_SECTION is 20 bytes of guest memory and a Win32 one is
 * 40, so the native lock cannot live in the guest struct. It lives here and is
 * found by the guest address it belongs to.
 *
 * KEYED ON THE GUEST VA MASKED TO 64 MB, not on the native pointer. The Xbox
 * memory controller has a 26-bit address bus, so the layout maps 28 mirror
 * views that alias the same physical pages - 0x00123456 and 0x20123456 are one
 * object. Keying on the pointer would hand two threads two different locks for
 * the same critical section and protect nothing, which is the kind of bug that
 * only shows up under load.
 *
 * The guest's own 20 bytes are deliberately left UNTOUCHED. The no-op version
 * never wrote them and the game reaches its current depth regardless, so the
 * field layout is unverified; writing a guessed LockCount would be a change
 * with no evidence behind it. If game code ever turns out to read those fields,
 * that is the point to establish the layout properly.
 */

#include "xbox_memory_layout.h"   /* XBOX_TOTAL_RAM */

extern ptrdiff_t g_xbox_mem_offset;

#if defined(_WIN32)
typedef CRITICAL_SECTION cs_native_t;
#define CS_INIT(m)          InitializeCriticalSection(m)
#define CS_ENTER(m)         EnterCriticalSection(m)
#define CS_LEAVE(m)         LeaveCriticalSection(m)
/* SRWLOCK, not CRITICAL_SECTION, for the table: it has a static initialiser,
 * so the table needs no bootstrap call before first use. */
typedef SRWLOCK cs_tablelock_t;
#define CS_TABLE_INIT       SRWLOCK_INIT
#define CS_TABLE_RLOCK(l)   AcquireSRWLockShared(l)
#define CS_TABLE_RUNLOCK(l) ReleaseSRWLockShared(l)
#define CS_TABLE_WLOCK(l)   AcquireSRWLockExclusive(l)
#define CS_TABLE_WUNLOCK(l) ReleaseSRWLockExclusive(l)
#define CS_SELF()           ((unsigned long)GetCurrentThreadId())
#else
#include <pthread.h>
typedef pthread_mutex_t cs_native_t;
static void cs_init_recursive(pthread_mutex_t *m)
{
    pthread_mutexattr_t a;
    pthread_mutexattr_init(&a);
    /* Xbox critical sections are recursive (they carry a RecursionCount), and
     * so is a Win32 CRITICAL_SECTION. Match that or self-nesting deadlocks. */
    pthread_mutexattr_settype(&a, PTHREAD_MUTEX_RECURSIVE);
    pthread_mutex_init(m, &a);
    pthread_mutexattr_destroy(&a);
}
#define CS_INIT(m)          cs_init_recursive(m)
#define CS_ENTER(m)         pthread_mutex_lock(m)
#define CS_LEAVE(m)         pthread_mutex_unlock(m)
typedef pthread_rwlock_t cs_tablelock_t;
#define CS_TABLE_INIT       PTHREAD_RWLOCK_INITIALIZER
#define CS_TABLE_RLOCK(l)   pthread_rwlock_rdlock(l)
#define CS_TABLE_RUNLOCK(l) pthread_rwlock_unlock(l)
#define CS_TABLE_WLOCK(l)   pthread_rwlock_wrlock(l)
#define CS_TABLE_WUNLOCK(l) pthread_rwlock_unlock(l)
#define CS_SELF()           ((unsigned long)pthread_self())
#endif

/* ponytail: fixed 256 slots, linear-probed, never freed. The game initialises
 * 15. Overflow fails OPEN (no lock taken) rather than blocking, and says so
 * once - a missing lock is recoverable, a deadlock in startup is not. Make it
 * growable if a title ever exceeds this. */
#define CS_SLOTS 256

typedef struct {
    uint32_t      key;      /* canonical guest VA; 0 = empty slot */
    unsigned long owner;    /* thread id currently holding it, 0 = free */
    unsigned      depth;    /* recursion depth, for the balance check */
    cs_native_t   lock;
} cs_slot_t;

static cs_slot_t      g_cs_slots[CS_SLOTS];
static cs_tablelock_t g_cs_table = CS_TABLE_INIT;
static int            g_cs_overflow_logged = 0;
static int            g_cs_unbalanced_logged = 0;

/* Canonical key for a critical section, collapsing the RAM mirrors. */
static uint32_t cs_key(const void *p)
{
    uintptr_t va;
    if (!p) return 0;
    va = (uintptr_t)((const char *)p - g_xbox_mem_offset);
    va &= (uintptr_t)(XBOX_TOTAL_RAM - 1);   /* 64 MB, a power of two */
    /* 0 marks an empty slot, so fold an (implausible) section at guest VA 0
     * onto 1 rather than making it unrepresentable. */
    return (uint32_t)va ? (uint32_t)va : 1u;
}

/* Slots are never removed, so a run of occupied slots ending in an empty one
 * proves the key is absent - the probe can stop there. */
static cs_slot_t *cs_probe(uint32_t key)
{
    uint32_t i, h = (key * 2654435761u) & (CS_SLOTS - 1);
    for (i = 0; i < CS_SLOTS; i++) {
        cs_slot_t *s = &g_cs_slots[(h + i) & (CS_SLOTS - 1)];
        if (s->key == key) return s;
        if (s->key == 0)   return NULL;
    }
    return NULL;
}

static cs_slot_t *cs_slot_for(const void *p, int create)
{
    uint32_t key = cs_key(p);
    cs_slot_t *s;
    if (!key) return NULL;

    CS_TABLE_RLOCK(&g_cs_table);
    s = cs_probe(key);
    CS_TABLE_RUNLOCK(&g_cs_table);
    if (s || !create) return s;

    CS_TABLE_WLOCK(&g_cs_table);
    s = cs_probe(key);              /* another thread may have won the race */
    if (!s) {
        uint32_t i, h = (key * 2654435761u) & (CS_SLOTS - 1);
        for (i = 0; i < CS_SLOTS; i++) {
            cs_slot_t *c = &g_cs_slots[(h + i) & (CS_SLOTS - 1)];
            if (c->key == 0) {
                CS_INIT(&c->lock);
                c->owner = 0;
                c->depth = 0;
                c->key = key;       /* published last */
                s = c;
                break;
            }
        }
    }
    CS_TABLE_WUNLOCK(&g_cs_table);

    if (!s && !g_cs_overflow_logged) {
        g_cs_overflow_logged = 1;
        fprintf(stderr, "  [RTL] critical-section table full at %d entries - "
                        "further sections run UNLOCKED. Raise CS_SLOTS.\n",
                CS_SLOTS);
        fflush(stderr);
    }
    return s;
}

VOID __stdcall xbox_RtlEnterCriticalSection(PRTL_CRITICAL_SECTION CriticalSection)
{
    /* create=1: game code enters sections that were never passed to
     * Initialize - statically initialised ones living in .data. Refusing to
     * lock those would leave exactly the structures most likely to be shared
     * unprotected. */
    cs_slot_t *s = cs_slot_for(CriticalSection, 1);
    if (!s) return;                 /* table full - documented fail-open */
    CS_ENTER(&s->lock);
    s->owner = CS_SELF();           /* safe: we hold the lock */
    s->depth++;
}

VOID __stdcall xbox_RtlLeaveCriticalSection(PRTL_CRITICAL_SECTION CriticalSection)
{
    cs_slot_t *s = cs_slot_for(CriticalSection, 0);
    if (!s) return;                 /* never entered - nothing to release */

    /* Releasing a lock this thread does not own is undefined behaviour, and it
     * would corrupt the lock rather than fail loudly. A no-op version could
     * never have exposed an unbalanced Leave in the game, so this checks
     * rather than assumes. */
    if (s->owner != CS_SELF() || s->depth == 0) {
        if (!g_cs_unbalanced_logged) {
            g_cs_unbalanced_logged = 1;
            fprintf(stderr, "  [RTL] LeaveCriticalSection on a section this "
                            "thread does not hold (key=0x%08X) - ignored\n",
                    s->key);
            fflush(stderr);
        }
        return;
    }
    if (--s->depth == 0) s->owner = 0;
    CS_LEAVE(&s->lock);
}

VOID __stdcall xbox_RtlInitializeCriticalSection(PRTL_CRITICAL_SECTION CriticalSection)
{
    (void)cs_slot_for(CriticalSection, 1);
}

/* ============================================================================
 * NTSTATUS → Win32 Error Code Mapping
 * ============================================================================ */

ULONG __stdcall xbox_RtlNtStatusToDosError(NTSTATUS Status)
{
    switch (Status) {
        case STATUS_SUCCESS:                    return ERROR_SUCCESS;
        case STATUS_INVALID_PARAMETER:          return ERROR_INVALID_PARAMETER;
        case STATUS_NO_MEMORY:                  return ERROR_NOT_ENOUGH_MEMORY;
        case STATUS_INSUFFICIENT_RESOURCES:     return ERROR_NO_SYSTEM_RESOURCES;
        case STATUS_ACCESS_DENIED:              return ERROR_ACCESS_DENIED;
        case STATUS_OBJECT_NAME_NOT_FOUND:      return ERROR_FILE_NOT_FOUND;
        case STATUS_OBJECT_PATH_NOT_FOUND:      return ERROR_PATH_NOT_FOUND;
        case STATUS_OBJECT_NAME_COLLISION:      return ERROR_ALREADY_EXISTS;
        case STATUS_NO_SUCH_FILE:               return ERROR_FILE_NOT_FOUND;
        case STATUS_END_OF_FILE:                return ERROR_HANDLE_EOF;
        case STATUS_INVALID_HANDLE:             return ERROR_INVALID_HANDLE;
        case STATUS_NOT_IMPLEMENTED:            return ERROR_CALL_NOT_IMPLEMENTED;
        case STATUS_UNSUCCESSFUL:               return ERROR_GEN_FAILURE;
        case STATUS_PENDING:                    return ERROR_IO_PENDING;
        case STATUS_BUFFER_OVERFLOW:            return ERROR_MORE_DATA;
        case STATUS_NO_MORE_FILES:              return ERROR_NO_MORE_FILES;
        case STATUS_NOT_SUPPORTED:              return ERROR_NOT_SUPPORTED;
        case STATUS_CANCELLED:                  return ERROR_CANCELLED;
        case STATUS_ALREADY_COMMITTED:          return ERROR_COMMITMENT_LIMIT;
        default:
            /* Fall back to RtlNtStatusToDosError from ntdll if available */
            xbox_log(XBOX_LOG_WARN, XBOX_LOG_RTL,
                "RtlNtStatusToDosError: unmapped status 0x%08X", Status);
            return ERROR_MR_MID_NOT_FOUND;
    }
}

/* ============================================================================
 * Time Conversion
 * ============================================================================ */

BOOLEAN __stdcall xbox_RtlTimeFieldsToTime(PXBOX_TIME_FIELDS TimeFields, PLARGE_INTEGER Time)
{
    SYSTEMTIME st;
    FILETIME ft;

    st.wYear         = (WORD)TimeFields->Year;
    st.wMonth        = (WORD)TimeFields->Month;
    st.wDayOfWeek    = (WORD)TimeFields->Weekday;
    st.wDay          = (WORD)TimeFields->Day;
    st.wHour         = (WORD)TimeFields->Hour;
    st.wMinute       = (WORD)TimeFields->Minute;
    st.wSecond       = (WORD)TimeFields->Second;
    st.wMilliseconds = (WORD)TimeFields->Milliseconds;

    if (!SystemTimeToFileTime(&st, &ft))
        return FALSE;

    Time->LowPart  = ft.dwLowDateTime;
    Time->HighPart = ft.dwHighDateTime;
    return TRUE;
}

VOID __stdcall xbox_RtlTimeToTimeFields(PLARGE_INTEGER Time, PXBOX_TIME_FIELDS TimeFields)
{
    FILETIME ft;
    SYSTEMTIME st;

    ft.dwLowDateTime  = Time->LowPart;
    ft.dwHighDateTime = Time->HighPart;

    if (FileTimeToSystemTime(&ft, &st)) {
        TimeFields->Year         = (SHORT)st.wYear;
        TimeFields->Month        = (SHORT)st.wMonth;
        TimeFields->Day          = (SHORT)st.wDay;
        TimeFields->Hour         = (SHORT)st.wHour;
        TimeFields->Minute       = (SHORT)st.wMinute;
        TimeFields->Second       = (SHORT)st.wSecond;
        TimeFields->Milliseconds = (SHORT)st.wMilliseconds;
        TimeFields->Weekday      = (SHORT)st.wDayOfWeek;
    } else {
        memset(TimeFields, 0, sizeof(XBOX_TIME_FIELDS));
    }
}

/* ============================================================================
 * Exception Handling
 * ============================================================================ */

VOID __stdcall xbox_RtlUnwind(PVOID TargetFrame, PVOID TargetIp, PVOID ExceptionRecord, PVOID ReturnValue)
{
    /* Delegate to Win32 RtlUnwind */
    RtlUnwind(TargetFrame, TargetIp, (PEXCEPTION_RECORD)ExceptionRecord, ReturnValue);
}

VOID __stdcall xbox_RtlRaiseException(PVOID ExceptionRecord)
{
    RaiseException(
        ((PEXCEPTION_RECORD)ExceptionRecord)->ExceptionCode,
        ((PEXCEPTION_RECORD)ExceptionRecord)->ExceptionFlags,
        ((PEXCEPTION_RECORD)ExceptionRecord)->NumberParameters,
        ((PEXCEPTION_RECORD)ExceptionRecord)->ExceptionInformation);
}

VOID __stdcall xbox_RtlRip(PCHAR ApiName, PCHAR Expression, PCHAR Message)
{
    xbox_log(XBOX_LOG_ERROR, XBOX_LOG_RTL, "RtlRip: %s - %s: %s",
        ApiName ? ApiName : "?",
        Expression ? Expression : "?",
        Message ? Message : "?");

#ifdef _DEBUG
    DebugBreak();
#endif
}

/* ============================================================================
 * String Formatting (Rtl sprintf variants → CRT)
 * ============================================================================ */

int __cdecl xbox_RtlSnprintf(char* buffer, size_t count, const char* format, ...)
{
    va_list args;
    va_start(args, format);
    int result = vsnprintf(buffer, count, format, args);
    va_end(args);
    return result;
}

int __cdecl xbox_RtlSprintf(char* buffer, const char* format, ...)
{
    va_list args;
    va_start(args, format);
    int result = vsprintf(buffer, format, args);
    va_end(args);
    return result;
}

int __cdecl xbox_RtlVsnprintf(char* buffer, size_t count, const char* format, va_list argptr)
{
    return vsnprintf(buffer, count, format, argptr);
}

int __cdecl xbox_RtlVsprintf(char* buffer, const char* format, va_list argptr)
{
    return vsprintf(buffer, format, argptr);
}
