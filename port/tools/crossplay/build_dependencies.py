"""Build pinned standalone GNS with native ICE/TURN, outside the checkout.

Requires CMake, Ninja, Git and a C++ toolchain. Cross builds require host protoc
36.1 and a target OpenSSL prefix (Android); Windows uses BCrypt and SChannel.
"""
import argparse
from pathlib import Path
import shutil
import subprocess as sp

GNS_REV = "d534c19aa760df3fb75fd20db13ba1932b8a5463"
PROTOBUF_REV = "f377bfefc5e2cfab68b816903c25b23e091c439d"  # v36.1


def run(*args):
    sp.run([str(x) for x in args], check=True)


def checkout(cache, name, repo, revision, patch=None):
    path = cache / name
    if not path.exists():
        run("git", "init", path)
        run("git", "-C", path, "remote", "add", "origin", repo)
        run("git", "-C", path, "fetch", "--depth=1", "origin", revision)
        run("git", "-C", path, "checkout", "--detach", "FETCH_HEAD")
    head = sp.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    dirty = sp.check_output(["git", "-C", str(path), "status", "--porcelain"], text=True).strip()
    if head == revision and patch and dirty:
        actual = sp.check_output(["git", "-C", str(path), "diff"], text=True)
        if actual == patch.read_text() and len(dirty.splitlines()) == 1:
            return path
    if head != revision or dirty:
        raise RuntimeError(f"Expected a clean pinned dependency at {path}; use a separate cache")
    if patch:
        run("git", "-C", path, "apply", "--check", patch)
        run("git", "-C", path, "apply", patch)
    return path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--target", choices=["native", "android", "windows"], default="native")
    p.add_argument("--prefix", type=Path, required=True)
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--toolchain", type=Path)
    p.add_argument("--openssl", type=Path)
    p.add_argument("--protoc", type=Path)
    p.add_argument("--cmake-arg", action="append", default=[], help="Extra definition, e.g. --cmake-arg=-DTTT2_WINSDK=/path")
    p.add_argument("--jobs", type=int, default=4)
    a = p.parse_args()
    prefix, cache = a.prefix.resolve(), a.cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    common = ["-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_TRY_COMPILE_CONFIGURATION=Release", "-DCMAKE_CXX_STANDARD=17",
              "-DCMAKE_POSITION_INDEPENDENT_CODE=ON", f"-DCMAKE_INSTALL_PREFIX={prefix}"]
    if a.toolchain:
        common += [f"-DCMAKE_TOOLCHAIN_FILE={a.toolchain.resolve()}"]
    if a.target == "android":
        if not a.toolchain or not a.openssl:
            p.error("Android requires --toolchain (NDK CMake file) and --openssl")
        common += ["-DANDROID_ABI=arm64-v8a", "-DANDROID_PLATFORM=android-28", "-DANDROID_STL=c++_shared"]
    if a.target == "windows":
        # Abseil's install-time ABI selection must see the real language version
        # with MSVC and clang-cl; otherwise its headers and protobuf can disagree.
        flags = cache / "msvc_language.cmake"
        flags.write_text('if(MSVC)\n  string(APPEND CMAKE_CXX_FLAGS " /Zc:__cplusplus")\nendif()\n')
        common += [f"-DCMAKE_PROJECT_INCLUDE={flags}", "-Dprotobuf_MSVC_STATIC_RUNTIME=OFF"]
    if a.target != "native":
        protoc = a.protoc or Path(shutil.which("protoc") or "")
        if not protoc.is_file() or sp.check_output([str(protoc), "--version"], text=True).strip() != "libprotoc 36.1":
            p.error("Cross builds require host protoc 36.1; build the native dependencies first and pass --protoc")
        common += [f"-DCMAKE_FIND_ROOT_PATH={prefix}", "-DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=ONLY"]
    common += a.cmake_arg
    protobuf = checkout(cache, "protobuf", "https://github.com/protocolbuffers/protobuf.git", PROTOBUF_REV)
    pb_build = cache / ("protobuf-"+a.target)
    run("cmake", "-S", protobuf, "-B", pb_build, *common, "-Dprotobuf_BUILD_TESTS=OFF",
        "-Dprotobuf_FORCE_FETCH_DEPENDENCIES=ON", "-Dprotobuf_BUILD_LIBUPB=OFF", "-Dprotobuf_WITH_ZLIB=OFF", "-DABSL_PROPAGATE_CXX_STD=ON",
        "-Dprotobuf_BUILD_PROTOC_BINARIES="+("ON" if a.target == "native" else "OFF"))
    run("cmake", "--build", pb_build, "--parallel", a.jobs)
    run("cmake", "--install", pb_build)
    if a.target == "native":
        protoc = prefix / "bin" / "protoc"
    gns = checkout(cache, "gns", "https://github.com/ValveSoftware/GameNetworkingSockets.git", GNS_REV, Path(__file__).resolve().with_name("gns-android-addresses.patch"))
    gns_build = cache / ("gns-"+a.target)
    extra = [f"-DCMAKE_PREFIX_PATH={prefix}", f"-DGNS_HOST_PROTOC={protoc}", f"-DProtobuf_PROTOC_EXECUTABLE={protoc}"]
    if a.target == "windows":
        extra += ["-DUSE_CRYPTO=BCrypt", "-DUSE_CRYPTO25519=Reference"]
    elif a.openssl:
        ssl = a.openssl.resolve()
        extra += [f"-DOPENSSL_INCLUDE_DIR={ssl}/include", f"-DOPENSSL_CRYPTO_LIBRARY={ssl}/lib/libcrypto.a",
                  f"-DOPENSSL_SSL_LIBRARY={ssl}/lib/libssl.a"]
    run("cmake", "-S", gns, "-B", gns_build, *common, *extra, "-DBUILD_EXAMPLES=OFF", "-DBUILD_TESTS=OFF",
        "-DBUILD_SHARED_LIB=OFF", "-DENABLE_ICE=ON", "-DUSE_STEAMWEBRTC=OFF")
    run("cmake", "--build", gns_build, "--parallel", a.jobs)
    run("cmake", "--install", gns_build)
    (prefix / "ttt2-crossplay-versions.txt").write_text(f"GNS={GNS_REV}\nprotobuf={PROTOBUF_REV}\nICE=native\n")
    for label, source in (("gns", gns), ("protobuf", protobuf), ("abseil", pb_build / "_deps/absl-src")):
        for file in source.rglob("*"):
            if file.is_file() and file.name.upper().split(".")[0] in {"LICENSE", "COPYING", "NOTICE"} and ".git" not in file.parts:
                target = prefix / "share/licenses/ttt2-crossplay" / label / file.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(file, target)
    print(f"Configure the game with -DTTT2_CROSSPLAY=ON -DCMAKE_PREFIX_PATH={prefix}")


if __name__ == "__main__":
    main()
