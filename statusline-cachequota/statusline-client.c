/* Claude Code status line client.
 *
 * Millisecond-scale front end for statusline-render.py --daemon: connects
 * to ~/.claude/statusline.sock, forwards stdin (the harness's JSON), and
 * prints the rendered line. If no daemon answers, it spawns one (detached)
 * and retries briefly; if the socket still cannot be reached, it execs the
 * one-shot python renderer so the statusline never goes blank.
 *
 * Build:  cc -O2 -o ~/.claude/statusline-client statusline-client.c
 *         (install.sh does this, and honours CLAUDE_CONFIG_DIR the same way.)
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>

static char sock_path[512];
static char script_path[512];

static int try_connect(void) {
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0)
        return -1;
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, sock_path, sizeof addr.sun_path - 1);
    if (connect(fd, (struct sockaddr *)&addr, sizeof addr) == 0)
        return fd;
    close(fd);
    return -1;
}

static void spawn_daemon(void) {
    pid_t pid = fork();
    if (pid != 0) {
        /* parent: reap the intermediate child immediately */
        if (pid > 0)
            waitpid(pid, NULL, 0);
        return;
    }
    /* child: detach fully, then become the daemon */
    if (fork() != 0)
        _exit(0);
    setsid();
    int devnull = open("/dev/null", O_RDWR);
    if (devnull >= 0) {
        dup2(devnull, 0);
        dup2(devnull, 1);
        dup2(devnull, 2);
        if (devnull > 2)
            close(devnull);
    }
    execlp("python3", "python3", "-ES", script_path, "--daemon", (char *)NULL);
    _exit(1);
}

int main(void) {
    const char *home = getenv("HOME");
    const char *cfg = getenv("CLAUDE_CONFIG_DIR");
    if (!home)
        home = "";
    if (cfg && *cfg) {
        snprintf(sock_path, sizeof sock_path, "%s/statusline.sock", cfg);
        snprintf(script_path, sizeof script_path, "%s/statusline-render.py", cfg);
    } else {
        snprintf(sock_path, sizeof sock_path, "%s/.claude/statusline.sock", home);
        snprintf(script_path, sizeof script_path, "%s/.claude/statusline-render.py", home);
    }

    /* Read all of stdin first (the payload is a few KB of JSON). */
    static char buf[262144];
    size_t len = 0;
    ssize_t n;
    while (len < sizeof buf
           && (n = read(0, buf + len, sizeof buf - len)) > 0)
        len += (size_t)n;

    int fd = try_connect();
    if (fd < 0) {
        spawn_daemon();
        for (int i = 0; i < 40 && fd < 0; i++) { /* up to ~400ms */
            usleep(10000);
            fd = try_connect();
        }
    }
    if (fd < 0) {
        /* Daemonless fallback: hand stdin to the one-shot renderer. The write
         * happens after the fork, with the renderer already draining the pipe,
         * so a payload larger than the pipe buffer cannot deadlock here. */
        int pipefd[2];
        if (pipe(pipefd) != 0)
            return 1;
        pid_t pid = fork();
        if (pid == 0) {
            close(pipefd[1]);
            dup2(pipefd[0], 0);
            close(pipefd[0]);
            execlp("python3", "python3", "-ES", script_path, (char *)NULL);
            _exit(1);
        }
        if (pid < 0)
            return 1;
        close(pipefd[0]);
        for (size_t off = 0; off < len; ) {
            ssize_t w = write(pipefd[1], buf + off, len - off);
            if (w <= 0)
                break;
            off += (size_t)w;
        }
        close(pipefd[1]);
        int status = 0;
        waitpid(pid, &status, 0);
        return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
    }

    if (write(fd, buf, len) < 0) { /* daemon still answers with best effort */ }
    shutdown(fd, SHUT_WR);
    while ((n = read(fd, buf, sizeof buf)) > 0) {
        if (write(1, buf, (size_t)n) < 0)
            break;
    }
    close(fd);
    return 0;
}
