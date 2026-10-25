#include <image.h>
#include <xex_patcher.h>
#include <fstream>
#include <iterator>
#include <stdexcept>

int main(int argc, char** argv) {
    if (argc != 5) return 2;
    try {
        if (XexPatcher::apply(argv[1], argv[2], argv[3]) != XexPatcher::Result::Success)
            return 3;
        std::ifstream input(argv[3], std::ios::binary);
        std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(input)), {});
        if (bytes.size() < 4) return 4;
        auto image = Image::ParseImage(bytes.data(), bytes.size());
        if (!image.data || image.base != 0x82000000 || !image.size) return 5;
        std::ofstream output(argv[4], std::ios::binary);
        output.write(reinterpret_cast<const char*>(image.data.get()), image.size);
        return output.good() ? 0 : 6;
    } catch (...) {
        return 7;
    }
}
