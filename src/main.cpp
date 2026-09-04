#include <cstdio>
#include <cstring>

#include "runtime.h"

namespace {

void print_usage() {
    std::printf(
        "MMBN3WhiteRecomp [--bios <path>] [--rom <path>] [game.toml]\n"
        "The BIOS and ROM must match the SHA-1 identities in game.toml.\n");
}

}  // namespace

int main(int argc, char** argv) {
    for (int i = 1; i < argc; ++i) {
        if (std::strcmp(argv[i], "--help") == 0 ||
            std::strcmp(argv[i], "-h") == 0) {
            print_usage();
            return 0;
        }
    }

    gbarecomp::RunOptions opts;
    opts.builtin_game_name = "Mega Man Battle Network 3 White";
    opts.builtin_rom_sha1 = "ff45038ae6d01cde4eae25a02dcb8bed29e07a6f";
    opts.builtin_rom_crc32 = 0x0be4410au;
    opts.mod_game_id = "mmbn3-white-us";
    opts.max_view_width = 240;

    // TODO: wire opts into runtime boot once generated/ exists.
    // Initial bring-up runs via gba_recompile + LLE oracle first.
    std::printf("%s: scaffold only — populate generated/ via tools/regen.sh\n",
                opts.builtin_game_name);
    return 0;
}
