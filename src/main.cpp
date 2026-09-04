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
    // CRC32 of the pinned White USA dump (same dump the SHA-1 gates on).
    opts.builtin_rom_crc32 = 0x0be4410au;
    // No mod catalog yet; faithful 240x160 only for initial bring-up.
    opts.max_view_width = 240;
    opts.launcher_region = "USA";
    opts.launcher_game_config = "game.toml";  // prefill ROM/BIOS from [rom]/[bios]
    opts.launcher_save_path = "saves/mmbn3_white_usa.sav";  // game.toml [save].path

    return gbarecomp::run_game(argc, argv, opts);
}
