/*
 * Merge an OS cryptex volume into an iOS System volume from a restore guest.
 *
 * Host-side traversal of the ~500,000-file System volume has previously
 * triggered APFS/fseventsd panics on the development host.  This helper runs
 * as root inside the guest instead.  It overlays the cryptex tree, replaces
 * System-volume graft symlinks with real directories when required, and uses
 * copyfile(3) to retain Apple metadata along with file contents.
 */

#include <copyfile.h>
#include <errno.h>
#include <fts.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

struct totals {
    uint64_t dirs;
    uint64_t files;
    uint64_t symlinks;
    uint64_t bytes;
};

static void fail_path(const char *what, const char *path)
{
    fprintf(stderr, "cryptex-merge: %s %s: %s\n", what, path,
            strerror(errno));
    exit(1);
}

static void destination_path(char out[PATH_MAX], const char *root,
                             const char *relative)
{
    int n = snprintf(out, PATH_MAX, "%s%s", root, relative);
    if (n < 0 || n >= PATH_MAX) {
        errno = ENAMETOOLONG;
        fail_path("destination path too long", relative);
    }
}

static void set_root_owner(const char *path)
{
    if (lchown(path, 0, 0) != 0) {
        fail_path("lchown", path);
    }
}

static void remove_non_directory(const char *path)
{
    struct stat st;
    if (lstat(path, &st) != 0) {
        if (errno == ENOENT) {
            return;
        }
        fail_path("lstat", path);
    }
    if (S_ISDIR(st.st_mode)) {
        errno = EISDIR;
        fail_path("refusing to replace directory", path);
    }
    if (unlink(path) != 0) {
        fail_path("unlink", path);
    }
}

static void ensure_directory(const char *path)
{
    struct stat st;
    if (lstat(path, &st) == 0) {
        if (S_ISDIR(st.st_mode)) {
            return;
        }
        if (unlink(path) != 0) {
            fail_path("replace graft symlink", path);
        }
    } else if (errno != ENOENT) {
        fail_path("lstat", path);
    }
    if (mkdir(path, 0700) != 0) {
        fail_path("mkdir", path);
    }
    set_root_owner(path);
}

static void copy_item(const char *source, const char *destination)
{
    remove_non_directory(destination);
    copyfile_flags_t flags = COPYFILE_ALL | COPYFILE_NOFOLLOW_SRC |
                             COPYFILE_NOFOLLOW_DST;
    if (copyfile(source, destination, NULL, flags) != 0) {
        fail_path("copyfile", source);
    }
    /* The host staging volume presents Apple-owned files as uid/gid 99 to
     * iOS. Normalize after copying inside the root guest; dyld rejects a
     * byte-perfect shared cache whose inode is not root-owned. */
    set_root_owner(destination);
}

static void copy_directory_metadata(const char *source, const char *destination)
{
    copyfile_flags_t flags = COPYFILE_STAT | COPYFILE_ACL | COPYFILE_XATTR |
                             COPYFILE_NOFOLLOW_SRC | COPYFILE_NOFOLLOW_DST;
    if (copyfile(source, destination, NULL, flags) != 0) {
        fail_path("copy directory metadata", source);
    }
    set_root_owner(destination);
}

static void merge_tree(const char *source_root, const char *destination_root,
                       struct totals *totals)
{
    char *paths[] = {(char *)source_root, NULL};
    FTS *tree = fts_open(paths, FTS_PHYSICAL | FTS_XDEV | FTS_NOCHDIR, NULL);
    if (!tree) {
        fail_path("fts_open", source_root);
    }
    size_t root_len = strlen(source_root);
    FTSENT *entry;
    uint64_t last_progress = 0;
    for (;;) {
        errno = 0;
        entry = fts_read(tree);
        if (!entry) {
            if (errno != 0) {
                fail_path("fts_read", source_root);
            }
            break;
        }
        const char *relative = entry->fts_path + root_len;
        char destination[PATH_MAX];
        if (*relative == '\0') {
            continue;
        }
        destination_path(destination, destination_root, relative);
        switch (entry->fts_info) {
        case FTS_D:
            ensure_directory(destination);
            totals->dirs++;
            break;
        case FTS_DP:
            copy_directory_metadata(entry->fts_path, destination);
            break;
        case FTS_F:
            copy_item(entry->fts_path, destination);
            totals->files++;
            totals->bytes += (uint64_t)entry->fts_statp->st_size;
            break;
        case FTS_SL:
        case FTS_SLNONE:
            copy_item(entry->fts_path, destination);
            totals->symlinks++;
            break;
        case FTS_DEFAULT:
            copy_item(entry->fts_path, destination);
            break;
        case FTS_DNR:
        case FTS_ERR:
        case FTS_NS:
            errno = entry->fts_errno ? entry->fts_errno : EIO;
            fail_path("walk", entry->fts_path);
        default:
            break;
        }
        uint64_t leaves = totals->files + totals->symlinks;
        if (leaves / 500 > last_progress / 500) {
            fprintf(stderr,
                    "cryptex-merge: progress files=%llu symlinks=%llu bytes=%llu\n",
                    (unsigned long long)totals->files,
                    (unsigned long long)totals->symlinks,
                    (unsigned long long)totals->bytes);
            last_progress = leaves;
        }
    }
    if (fts_close(tree) != 0) {
        fail_path("fts_close", source_root);
    }
}

static void verify_tree(const char *source_root, const char *destination_root,
                        struct totals *totals)
{
    char *paths[] = {(char *)source_root, NULL};
    FTS *tree = fts_open(paths, FTS_PHYSICAL | FTS_XDEV | FTS_NOCHDIR, NULL);
    if (!tree) {
        fail_path("verify fts_open", source_root);
    }
    size_t root_len = strlen(source_root);
    FTSENT *entry;
    for (;;) {
        errno = 0;
        entry = fts_read(tree);
        if (!entry) {
            if (errno != 0) {
                fail_path("verify fts_read", source_root);
            }
            break;
        }
        const char *relative = entry->fts_path + root_len;
        char destination[PATH_MAX];
        struct stat target;
        if (*relative == '\0' || entry->fts_info == FTS_DP) {
            continue;
        }
        destination_path(destination, destination_root, relative);
        if (lstat(destination, &target) != 0) {
            fail_path("verify missing", destination);
        }
        if (target.st_uid != 0 || target.st_gid != 0) {
            errno = EPERM;
            fail_path("verify root ownership", destination);
        }
        switch (entry->fts_info) {
        case FTS_D:
            if (!S_ISDIR(target.st_mode)) {
                errno = ENOTDIR;
                fail_path("verify directory", destination);
            }
            totals->dirs++;
            break;
        case FTS_F:
            if (!S_ISREG(target.st_mode) ||
                target.st_size != entry->fts_statp->st_size) {
                errno = EIO;
                fail_path("verify file size/type", destination);
            }
            totals->files++;
            totals->bytes += (uint64_t)target.st_size;
            break;
        case FTS_SL:
        case FTS_SLNONE: {
            char source_link[PATH_MAX];
            char target_link[PATH_MAX];
            ssize_t source_len = readlink(entry->fts_path, source_link,
                                          sizeof(source_link) - 1);
            ssize_t target_len = readlink(destination, target_link,
                                          sizeof(target_link) - 1);
            if (source_len < 0 || target_len < 0 || source_len != target_len ||
                memcmp(source_link, target_link, (size_t)source_len) != 0) {
                errno = EIO;
                fail_path("verify symlink", destination);
            }
            totals->symlinks++;
            break;
        }
        default:
            break;
        }
    }
    if (fts_close(tree) != 0) {
        fail_path("verify fts_close", source_root);
    }
}

static int unsafe_manifest_path(const char *path)
{
    return path[0] != '/' || strstr(path, "/../") != NULL ||
           strcmp(path, "/..") == 0;
}

static void repair_owners(const char *manifest_path, const char *destination_root)
{
    FILE *manifest = fopen(manifest_path, "rb");
    if (!manifest) {
        fail_path("open manifest", manifest_path);
    }
    char *path = NULL;
    size_t capacity = 0;
    ssize_t length;
    uint64_t repaired = 0;
    while ((length = getdelim(&path, &capacity, '\0', manifest)) >= 0) {
        if (length == 0 || path[length - 1] != '\0' || unsafe_manifest_path(path)) {
            errno = EINVAL;
            fail_path("unsafe manifest path", path ? path : "(null)");
        }
        char destination[PATH_MAX];
        destination_path(destination, destination_root, path);
        struct stat target;
        if (lstat(destination, &target) != 0) {
            fail_path("repair missing", destination);
        }
        set_root_owner(destination);
        if (lstat(destination, &target) != 0 || target.st_uid != 0 || target.st_gid != 0) {
            errno = EPERM;
            fail_path("repair verify", destination);
        }
        repaired++;
    }
    if (ferror(manifest)) {
        fail_path("read manifest", manifest_path);
    }
    free(path);
    if (fclose(manifest) != 0) {
        fail_path("close manifest", manifest_path);
    }
    sync();
    printf("DVM_CRYPTEX_OWNER_REPAIR_OK paths=%llu\n",
           (unsigned long long)repaired);
}

int main(int argc, char **argv)
{
    if (argc == 4 && strcmp(argv[1], "--repair-owners") == 0 &&
        argv[2][0] == '/' && argv[3][0] == '/') {
        repair_owners(argv[2], argv[3]);
        return 0;
    }
    if (argc != 3 || argv[1][0] != '/' || argv[2][0] != '/') {
        fprintf(stderr, "usage: dvm-cryptex-merge SOURCE_ROOT DESTINATION_ROOT\n"
                        "       dvm-cryptex-merge --repair-owners MANIFEST DESTINATION_ROOT\n");
        return 2;
    }
    struct totals copied = {0};
    struct totals verified = {0};
    errno = 0;
    merge_tree(argv[1], argv[2], &copied);
    sync();
    errno = 0;
    verify_tree(argv[1], argv[2], &verified);
    if (memcmp(&copied, &verified, sizeof(copied)) != 0) {
        fprintf(stderr, "cryptex-merge: copy/verification totals differ\n");
        return 1;
    }
    printf("DVM_CRYPTEX_MERGE_OK dirs=%llu files=%llu symlinks=%llu bytes=%llu\n",
           (unsigned long long)verified.dirs,
           (unsigned long long)verified.files,
           (unsigned long long)verified.symlinks,
           (unsigned long long)verified.bytes);
    return 0;
}
