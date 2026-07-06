/* LecturePipeline.app 네이티브 런처.
 * 쉘 스크립트 실행 파일은 macOS가 Intel 앱으로 오인해 Rosetta를 요구하므로
 * arm64 Mach-O 바이너리로 venv 파이썬을 exec한다.
 * 빌드: cc -arch arm64 -O2 -o LecturePipeline.app/Contents/MacOS/LecturePipeline app/launcher.c
 */
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(void) {
    char exe[PATH_MAX];
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) != 0) return 1;

    char exedir[PATH_MAX];
    strlcpy(exedir, dirname(exe), sizeof(exedir));   /* .../Contents/MacOS */

    char rel[PATH_MAX], root[PATH_MAX];
    snprintf(rel, sizeof(rel), "%s/../../..", exedir);  /* .app이 있는 폴더 = 프로젝트 루트 */
    if (!realpath(rel, root)) return 1;

    char py[PATH_MAX], script[PATH_MAX];
    snprintf(py, sizeof(py), "%s/.venv/bin/python", root);
    snprintf(script, sizeof(script), "%s/app/app.py", root);

    execl(py, py, script, (char *)NULL);
    perror("LecturePipeline launcher: exec 실패");
    return 1;
}
