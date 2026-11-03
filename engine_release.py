"""Reviewed upstream inputs. Asset availability is NOT a platform support claim."""

VERSION = "0.2.3"
UPSTREAM_LICENSE = """MIT License

Copyright (c) 2021 MeetWq

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

Resource notice: upstream describes meme materials as sourced from the Internet.
Individual font/material licenses have NOT been fully verified. The software
license above is not a claim that all third-party resources are MIT licensed.
"""
COMMIT = "9c0607407675a2a92d4d24b5fbbeaf0cc119db67"
RESOURCE_ARCHIVE = f"https://codeload.github.com/MemeCrafters/meme-generator-rs/zip/{COMMIT}"
RESOURCE_ROOT = f"meme-generator-rs-{COMMIT}/"
# GitHub can regenerate ZIP compression. Trust the pinned manifest and each
# resource's digest, not the transport archive's bytes.
RESOURCE_SHA256 = "68a03183b74fffdb82abe1f49f5a9bf1b0e009ff09944646dfcd35f79b7f5d71"
RELEASE_BASE = f"https://github.com/MemeCrafters/meme-generator-rs/releases/download/v{VERSION}/"
# GitHub release asset digests, reviewed against the v0.2.3 API response.
ASSETS = {
    "linux-x86_64": (17130217, "e1fad7efd39e6ba51d422f42db1602ff50bfb709fc438d39280043884c2f731c"),
    "linux-aarch64": (17611302, "7f71382017a9bfdc7eb696a575e7ce524fafb666aa7907191f5907b92870127a"),
    "windows-x86_64": (13129960, "a8cae60d7d56c965ef0f6c8906c4c14ab0d6d07aef985d6ba62a499c875857a1"),
    "macos-x86_64": (14251750, "494c5191019b93b39aa82ec3bb49baab30c32e76bd39c7b626fe20a4e5ea9136"),
    "macos-aarch64": (14015286, "20b7da03da4fed206b0b0e3ef34094dfac433e380f444891f2e586318bf186a3"),
    "android-aarch64": (16268146, "934847cd4148a1a33b48874640d2633c33ec0ef4f3210bdd00817b27727610f1"),
}
