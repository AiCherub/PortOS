/* Regression fixture: a real Wine WinMM output, not Linux paplay.
 * Build: x86_64-w64-mingw32-gcc audio-tone.c -o audio-tone.exe -lwinmm
 * Run: wine audio-tone.exe Z:\\tmp\\tone.wav
 */
#include <windows.h>
#include <mmsystem.h>
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    return PlaySoundA(argv[1], NULL, SND_FILENAME | SND_SYNC | SND_NODEFAULT) ? 0 : 1;
}
