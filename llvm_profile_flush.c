#define _GNU_SOURCE

#include <link.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/*
 * aptos-node is a long-running process and terminates directly on signals, so
 * LLVM's normal atexit profile writer never runs.  This tiny preload helper
 * lets the experiment driver request a profile flush through a marker file.
 *
 * __llvm_profile_write_file is local to the PIE executable.  The driver reads
 * its ELF value with `nm` and supplies it in BCFUZZER_PROFILE_WRITE_OFFSET;
 * adding that value to the main executable's load bias gives the callable
 * address in this process.
 */

static uintptr_t main_load_bias;
static int found_main_executable;

static int find_main_executable(
    struct dl_phdr_info *info, size_t size, void *data)
{
    (void)size;
    (void)data;
    if (info->dlpi_name == NULL || info->dlpi_name[0] == '\0') {
        main_load_bias = (uintptr_t)info->dlpi_addr;
        found_main_executable = 1;
        return 1;
    }
    return 0;
}

static void *flush_when_requested(void *unused)
{
    (void)unused;
    const char *marker = getenv("BCFUZZER_PROFILE_FLUSH_MARKER");
    const char *offset_text = getenv("BCFUZZER_PROFILE_WRITE_OFFSET");
    if (marker == NULL || marker[0] == '\0' || offset_text == NULL ||
        offset_text[0] == '\0') {
        fprintf(stderr, "bcfuzzer-profile-flush: missing marker/offset\n");
        return NULL;
    }

    char *end = NULL;
    uintptr_t offset = (uintptr_t)strtoull(offset_text, &end, 0);
    if (end == offset_text || *end != '\0') {
        fprintf(stderr, "bcfuzzer-profile-flush: invalid offset %s\n",
                offset_text);
        return NULL;
    }
    dl_iterate_phdr(find_main_executable, NULL);
    if (!found_main_executable) {
        fprintf(stderr, "bcfuzzer-profile-flush: main executable not found\n");
        return NULL;
    }
    fprintf(stderr,
            "bcfuzzer-profile-flush: watching %s offset=0x%llx bias=0x%llx\n",
            marker, (unsigned long long)offset,
            (unsigned long long)main_load_bias);

    while (access(marker, F_OK) != 0) {
        usleep(100000);
    }

    int (*write_profile)(void) =
        (int (*)(void))(main_load_bias + offset);
    int result = write_profile();
    fprintf(stderr, "bcfuzzer-profile-flush: write result=%d\n", result);
    _exit(result == 0 ? 0 : 86);
}

__attribute__((constructor)) static void install_flush_watcher(void)
{
    if (getenv("BCFUZZER_PROFILE_FLUSH_MARKER") == NULL) {
        return;
    }
    pthread_t thread;
    if (pthread_create(&thread, NULL, flush_when_requested, NULL) == 0) {
        pthread_detach(thread);
    }
}
