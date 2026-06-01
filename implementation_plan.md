# **Implementation Blueprint: On-the-Fly TAR Stream Archiving in LocalSend**

The performance of peer-to-peer file transfers involving thousands of small files is bottlenecked by filesystem metadata transactions and HTTP connection setup times. Sequential transfers require the operating system kernel to perform N separate file allocations, directory modifications, and security-permission validations, leading to severe throughput drops (down to 80 KB/s on low-end devices).  
By streaming files as a single uncompressed tape archive (TAR) payload, the cumulative metadata overhead is reduced to O(1) on the network layer and sequential I/O is restored on the storage controller.  
This document details the architectural plan to implement streaming TAR transfers in LocalSend while maintaining backward compatibility with older clients using protocol negotiation.

## **1\. Handshake Protocol & Capability Negotiation**

To ensure that a newer LocalSend client does not send a unified TAR stream to an older client that expects traditional sequential uploads, we extend the LocalSend Protocol v2 handshake.  
                           
           `│                                                 │`  
           `│ ─── 1. POST /prepare-upload (tarSupported:true) ──► │`  
           `│                                                 │`  
           `│ ◄── 2. Response (tarSupported: true/false) ───────│`  
           `│                                                 │`  
           `▼                                                 ▼`  
                                         
     `If true: Send single TAR stream.                  If true: Extract incoming TAR stream.`  
     `If false: Fallback to file-by-file.               If false: Receive files sequentially.`

### **1.1 DTO Extensions**

We modify the handshake payload exchanged on the /api/localsend/v2/prepare-upload endpoint.
In this repo the DTOs live in:
- Dart common models: common/lib/model/dto/prepare_upload_request_dto.dart and common/lib/model/dto/prepare_upload_response_dto.dart
- Rust HTTP models: core/src/http/dto.rs and core/src/http/dto_v2.rs (used by the Rust HTTP client)

#### **Request Payload (PrepareUploadRequestDto)**

The sender advertises its capability to package transfers as an on-the-fly TAR stream by appending a tarSupported boolean field:  
`{`  
  `"info": {`  
    `"alias": "Nice Orange",`  
    `"version": "2.0",`  
    `"deviceModel": "Samsung",`  
    `"deviceType": "mobile",`  
    `"fingerprint": "9a1f...",`  
    `"port": 53317,`  
    `"protocol": "https"`  
  `},`  
  `"files": {`  
    `"file_id_1": {`  
      `"id": "file_id_1",`  
      `"fileName": "image1.jpg",`  
      `"size": 14032,`  
      `"fileType": "image/jpeg"`  
    `},`  
    `"file_id_2": {`  
      `"id": "file_id_2",`  
      `"fileName": "image2.jpg",`  
      `"size": 22104,`  
      `"fileType": "image/jpeg"`  
    `}`  
  `},`  
  `"tarSupported": true`  
`}`

#### **Response Payload (PrepareUploadResponseDto)**

* **Case A: Receiver supports TAR streaming.** The receiver acknowledges the optimization and returns a single unified token for the entire TAR session, rather than mapping individual tokens for each file:  
  `{`  
    `"sessionId": "session_uuid_1234",`  
    `"tarSupported": true,`  
    `"tarToken": "tar_session_token_5678",`  
    `"files": {}`  
  `}`

* **Case B: Receiver is an older client.** The receiver ignores the unrecognized "tarSupported" request parameter and responds with the standard protocol structure, mapping separate tokens to each unique file ID:  
  `{`  
    `"sessionId": "session_uuid_1234",`  
    `"files": {`  
      `"file_id_1": "token_abc123",`  
      `"file_id_2": "token_xyz789"`  
    `}`  
  `}`

## **2\. Sender-Side Implementation (send\_provider.dart)**

On the sender side, the transfer logic is managed in localsend/app/lib/provider/network/send\_provider.dart. We modify the execution path to conditionally branch based on the handshake negotiation response.

### **2.1 The Streaming TAR Transformer**

Using package:tar, we convert selected files into an asynchronous TAR byte stream on the fly. TAR header names use the fileId so the receiver can map each entry back to its ReceivingFile state:  
`import 'package:tar/tar.dart';`

`/// Generates an uncompressed asynchronous TAR byte stream from a list of SendingFile objects.`  
`Stream<List<int>> createTarStream(List<SendingFile> files) {`  
  `final entries = Stream.fromIterable(files).asyncMap((file) async {`  
    `final header = TarHeader(`  
      `name: file.file.id,`  
      `mode: 0o644,`  
      `size: file.file.size,`  
      `modified: file.file.metadata?.lastModified ?? DateTime.now(),`  
    `);`  
    `return TarEntry(header, openFileStream(file));`  
  `});`  
  `return entries.transform(tarWriter);`  
`}`

### **2.2 Integration with the Sending Route**

In the session-handling method inside send\_provider.dart, evaluate the response metadata :  
`// app/lib/provider/network/send_provider.dart`

`Future<void> executeTransferSession({`  
  `required TargetNode target,`  
  `required List<File> filesToTransfer,`  
`}) async {`  
  `// 1. Prepare and send handshake payload`  
  `final requestDto = PrepareUploadRequestDto(`  
    `files: mapFilesToDto(filesToTransfer),`  
    `tarSupported: true, // Advertise optimization capability`  
  `);`  
    
  `final response = await rustClient.prepareUpload(target, requestDto);`

  `if (response.tarSupported == true && response.tarToken != null) {`  
    `// OPTIMIZED PATH: Send a single uncompressed TAR stream`  
    `final tarByteStream = createTarStream(filesToTransfer);`  
    `final totalSize = computeTarSize(filesToTransfer);`

    `await rhttpClient.request(`  
      `method: HttpMethod.post,`  
      `url: ApiRoute.upload.target(target, query: {`  
        `'sessionId': response.sessionId,`  
        `'tar': 'true',`  
        `'token': response.tarToken!,`  
      `}),`  
      `headers: {`  
        `'Content-Type': 'application/x-tar',`  
        `'Content-Length': totalSize.toString(),`  
      `},`  
      `body: HttpBody.stream(tarByteStream, length: totalSize),`  
      `onSendProgress: (sent, total) {`  
        `// Map aggregate stream bytes back to file progress`  
        `updateSessionProgress(response.sessionId, sent, total);`  
      `},`  
    `);`  
  `} else {`  
    `// COMPATIBILITY FALLBACK: Run the original sequential upload engine`  
    `for (final file in filesToTransfer) {`  
      `final fileToken = response.files[file.id];`  
      `await executeClassicUpload(target, response.sessionId, file, fileToken);`  
    `}`  
  `}`  
`}`

## **3\. Receiver-Side Implementation (receive\_controller.dart)**

The receiving endpoint is written in Dart in app/lib/provider/network/server/controller/receive\_controller.dart.  
We adjust the handler for the POST /api/localsend/v2/upload route to detect the tar=true query parameter and stream each TAR entry into saveFile().

### **3.1 Streaming TAR Extraction Logic**

When tar=true is set, we bypass the standard file-by-file upload handler. Instead, we feed the HttpRequest stream into TarReader and call saveFile() per entry, updating progress and history per file:  
`// app/lib/provider/network/server/controller/receive_controller.dart`

`import 'dart:typed_data';`  
`import 'package:tar/tar.dart';`

`Future<void> handleUpload(HttpRequest request) async {`  
  `final queryParams = request.uri.queryParameters;`  
  `final isTarStream = queryParams['tar'] == 'true';`  
  `final sessionId = queryParams['sessionId'];`  
  `final token = queryParams['token'];`

  `// Validate TAR session token`  
  `if (!validateTarToken(sessionId, token)) {`  
    `return await request.respondJson(403, message: 'Invalid token');`  
  `}`

  `if (isTarStream) {`  
    `final reader = TarReader(request);`  
    `while (await reader.moveNext()) {`  
      `final entry = reader.current;`  
      `final fileId = entry.header.name;`  
      `final receivingFile = lookupById(fileId);`  
      `final stream = entry.contents.map((chunk) => Uint8List.fromList(chunk));`  
      `await saveFile(..., stream: stream);`  
      `updateProgressAndHistory(fileId);`  
    }`  
    `await reader.cancel();`  
    `return await request.respondJson(200);`  
  } else {`  
    `return handleClassicFileUpload(request);`  
  }`  
`}`

## **4\. Systems Performance & Resource Impact**

By processing files sequentially within a single network stream, we optimize how the underlying operating system manages CPU resources, kernel transitions, and physical hardware writes:

1. **System Call Minimization (N\_{\\text{syscalls}}):** During a traditional transfer of N\_{\\text{files}} files, the host OS executes O(N\_{\\text{files}}) separate socket connection handshakes (sys\_connect, sys\_accept) and secure SSL/TLS sessions. Using the TAR stream optimization reduces the network-level execution complexity to: The operating system thread-scheduler does not need to repeatedly context-switch between user space and kernel space to bind new ephemeral ports. 2\. **Sequential Memory Management:** Since both createTarStream() and TarReader operate asynchronously chunk-by-chunk, memory consumption remains constant: where B represents the configured buffer chunk size (typically 64 KB, scaleable to 1 MB). This completely prevents heap balloons and Out-of-Memory (OOM) app terminations on low-end mobile devices during large transfers.  
2. **Storage Controller Optimization:** By feeding a continuous byte stream to the physical storage driver, the operating system's filesystem journal consolidates writes, allowing the controller to perform high-speed sequential allocation instead of seeking across random blocks on disk.

## **5\. Local Build Notes**

After adding the `tar` package, update dependencies and regenerate FRB bindings:

1. `fvm flutter pub upgrade --major-versions`
2. `cd app`
3. `flutter_rust_bridge_codegen generate`

If you see Rust compile errors like `missing field tar_supported` in `PrepareUploadRequestDto` or `PrepareUploadResponseDto`, ensure the FRB Rust mirror structs include the new TAR fields and re-run the generator.