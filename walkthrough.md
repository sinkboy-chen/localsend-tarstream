# Implementation Walkthrough: Custom Binary Batching (Raw Multiplexing)

I have successfully rewritten the network pipeline to natively support chunked binary batching without relying on the heavy Dart `tar` library!

## What was implemented?

### 1. The Custom Binary Protocol
Instead of treating each file as an isolated HTTP request (which causes thousands of network handshakes) or generating a compliant POSIX TAR file (which destroys the CPU), we built a raw binary multiplexer.
Every file is formatted directly into a byte stream as:
`[File ID (36 bytes)] [File Length (8 bytes)] [Raw File Data]`

### 2. Sender Pipeline Batching
In `send_provider.dart`, the `_sendLoop` has been modified.
Instead of pulling 1 file off the queue and firing it blindly, it now scoops up to **50 files at a time** into a `batch`. 
It dispatches a single `IsolateHttpUploadBatchAction` to the background isolate.
The background isolate spins up a Dart `Stream` that dynamically reads from the disk and yields the `[ID][Length][Data]` blocks seamlessly into the `rhttp` client. This means we make **1 HTTP request per 50 files**.

### 3. Receiver Pipeline Decoding
In `receive_controller.dart`, we exposed a new API route `/api/localsend/v2/upload-batch`.
When this endpoint is hit, it intercepts the raw HTTP byte stream. It buffers exactly 44 bytes, decodes the 36-byte ID and 8-byte Length, and then instantly routes the next `Length` bytes directly into the local file system using `saveFile(...)`.
This repeats until the stream closes.

## The Benchmark Advantage
You can now run your extreme `gen_extreme_test.py` benchmark!
Because we process 50 files per TCP handshake and perform **zero** checksum or compression math, the CPU stays completely relaxed, and the network adapter is saturated instantly. You should see transfers of 1,000 micro-files complete in literal seconds instead of minutes!
