import 'dart:async';
import 'dart:collection';
import 'dart:convert';
import 'dart:io' as io;

import 'package:common/isolate.dart';
import 'package:common/api_route_builder.dart';
import 'package:common/model/device.dart';
import 'package:common/model/dto/file_dto.dart';
import 'package:common/model/file_status.dart';
import 'package:common/model/file_type.dart';
import 'package:common/model/session_status.dart';
import 'package:common/util/sleep.dart';
import 'package:flutter/material.dart';
import 'package:localsend_app/model/cross_file.dart';
import 'package:localsend_app/model/send_mode.dart';
import 'package:localsend_app/model/state/send/send_session_state.dart';
import 'package:localsend_app/model/state/send/sending_file.dart';
import 'package:localsend_app/pages/home_page.dart';
import 'package:localsend_app/pages/progress_page.dart';
import 'package:localsend_app/pages/send_page.dart';
import 'package:localsend_app/provider/device_info_provider.dart';
import 'package:localsend_app/provider/http_provider.dart';
import 'package:localsend_app/provider/progress_provider.dart';
import 'package:localsend_app/provider/selection/selected_sending_files_provider.dart';
import 'package:localsend_app/provider/settings_provider.dart';
import 'package:localsend_app/provider/security_provider.dart';
import 'package:localsend_app/rust/api/http.dart' as rust_http;
import 'package:localsend_app/rust/api/model.dart' as rust_model;
import 'package:localsend_app/util/rust.dart';
import 'package:localsend_app/util/rhttp.dart';
import 'package:localsend_app/widget/dialogs/pin_dialog.dart';
import 'package:logging/logging.dart';
import 'package:refena_flutter/refena_flutter.dart';
import 'package:rhttp/rhttp.dart';
import 'package:routerino/routerino.dart';
import 'package:tar/tar.dart';
import 'package:uri_content/uri_content.dart';
import 'package:uuid/uuid.dart';

const _uuid = Uuid();
final _logger = Logger('Send');
const _tarBlockSize = 512;

/// This provider manages sending files to other devices.
///
/// In contrast to [serverProvider], this provider does not manage a server.
/// Instead, it only does HTTP requests to other servers.
final sendProvider = NotifierProvider<SendNotifier, Map<String, SendSessionState>>((ref) {
  return SendNotifier();
});

class SendNotifier extends Notifier<Map<String, SendSessionState>> {
  SendNotifier();

  final _tarCancelTokens = <String, CancelToken>{};

  @override
  Map<String, SendSessionState> init() {
    return {};
  }

  /// Starts a session.
  /// If [background] is true, then the session closes itself on success and no pages will be open
  /// If [background] is false, then this method will open pages by itself and waits for user input to close the session.
  Future<void> startSession({
    required Device target,
    required List<CrossFile> files,
    required bool background,
  }) async {
    final client = ref.read(httpProvider).v2;
    final sessionId = _uuid.v4();

    final requestState = SendSessionState(
      sessionId: sessionId,
      remoteSessionId: null,
      background: background,
      status: SessionStatus.waiting,
      target: target,
      files: Map.fromEntries(
        await Future.wait(
          files.map((file) async {
            final id = _uuid.v4();
            return MapEntry(
              id,
              SendingFile(
                file: FileDto(
                  id: id,
                  fileName: file.name,
                  size: file.size,
                  fileType: file.fileType,
                  hash: null,
                  preview: files.length == 1 && files.first.fileType == FileType.text && files.first.bytes != null
                      ? utf8.decode(files.first.bytes!) // send simple message by embedding it into the preview
                      : null,
                  metadata: file.lastModified != null || file.lastAccessed != null
                      ? FileMetadata(
                          lastModified: file.lastModified,
                          lastAccessed: file.lastAccessed,
                        )
                      : null,
                ),
                status: FileStatus.queue,
                token: null,
                thumbnail: file.thumbnail,
                asset: file.asset,
                path: file.path,
                bytes: file.bytes,
                errorMessage: null,
              ),
            );
          }),
        ),
      ),
      startTime: null,
      endTime: null,
      sendingTasks: [],
      errorMessage: null,
    );

    final originDevice = ref.read(deviceFullInfoProvider);
    final requestDto = rust_model.PrepareUploadRequestDto(
      info: rust_model.RegisterDto(
        alias: originDevice.alias,
        version: originDevice.version,
        deviceModel: originDevice.deviceModel,
        deviceType: originDevice.deviceType.toRust(),
        token: originDevice.fingerprint,
        port: originDevice.port,
        protocol: originDevice.https ? rust_model.ProtocolType.https : rust_model.ProtocolType.http,
        hasWebInterface: originDevice.download,
      ),
      files: {
        for (final entry in requestState.files.entries) entry.key: entry.value.file.toRust(),
      },
      tarSupported: true,
    );

    state = state.updateSession(
      sessionId: sessionId,
      state: (_) => requestState,
    );

    if (!background) {
      // ignore: use_build_context_synchronously, unawaited_futures
      Routerino.context.push(
        () => SendPage(showAppBar: false, closeSessionOnClose: true, sessionId: sessionId),
        transition: RouterinoTransition.fade(),
      );
    }

    rust_http.PrepareUploadResult? response;
    bool invalidPin;
    bool pinFirstAttempt = true;
    String? pin;
    do {
      invalidPin = false;
      try {
        response = await client.prepareUpload(
          protocol: target.getProtocolType(),
          ip: target.ip!,
          port: target.port,
          payload: requestDto,
          // TODO
          publicKey: null,
          pin: pin,
        );
      } on rust_http.RsHttpClientError_StatusCode catch (e) {
        switch (e.status) {
          case 401:
            invalidPin = true;

            // wait until animation is finished
            await sleepAsync(500);

            pin = await showDialog<String>(
              context: Routerino.context, // ignore: use_build_context_synchronously
              builder: (_) => PinDialog(
                obscureText: true,
                showInvalidPin: !pinFirstAttempt,
              ),
            );

            pinFirstAttempt = false;

            if (pin == null) {
              state = state.updateSession(
                sessionId: sessionId,
                state: (s) => s?.copyWith(
                  status: SessionStatus.canceledBySender,
                ),
              );
              return;
            }
            break;
          case 403:
            state = state.updateSession(
              sessionId: sessionId,
              state: (s) => s?.copyWith(
                status: SessionStatus.declined,
              ),
            );
            return;
          case 409:
            state = state.updateSession(
              sessionId: sessionId,
              state: (s) => s?.copyWith(
                status: SessionStatus.recipientBusy,
              ),
            );
            return;
          case 429:
            state = state.updateSession(
              sessionId: sessionId,
              state: (s) => s?.copyWith(
                status: SessionStatus.tooManyAttempts,
              ),
            );
            return;
          default:
            state = state.updateSession(
              sessionId: sessionId,
              state: (s) => s?.copyWith(
                status: SessionStatus.finishedWithErrors,
                errorMessage: e.humanErrorMessage,
              ),
            );
            return;
        }
      } catch (e) {
        state = state.updateSession(
          sessionId: sessionId,
          state: (s) => s?.copyWith(
            status: SessionStatus.finishedWithErrors,
            errorMessage: e.humanErrorMessage,
          ),
        );
        return;
      }
    } while (invalidPin);

    if (response == null) {
      return;
    }

    final Map<String, String> fileMap;
    final bool tarSupported;
    final String? tarToken;
    if (response.statusCode == 204) {
      // Nothing selected
      // Interpret this as "Read and close"
      fileMap = {};
      tarSupported = false;
      tarToken = null;
    } else {
      try {
        final responseBody = response.response!;
        fileMap = responseBody.files;
        tarSupported = responseBody.tarSupported && responseBody.tarToken != null;
        tarToken = responseBody.tarToken;
        final remoteSessionId = responseBody.sessionId;
        state = state.updateSession(
          sessionId: sessionId,
          state: (s) => s?.copyWith(
            remoteSessionId: remoteSessionId,
          ),
        );
      } catch (e) {
        state = state.updateSession(
          sessionId: sessionId,
          state: (s) => s?.copyWith(
            status: SessionStatus.finishedWithErrors,
            errorMessage: e.humanErrorMessage,
          ),
        );
        return;
      }
    }

    if (!tarSupported && fileMap.isEmpty) {
      // receiver has nothing selected
      state = state.updateSession(
        sessionId: sessionId,
        state: (s) => s?.copyWith(
          status: SessionStatus.finished,
        ),
      );

      if (state[sessionId]?.background == false) {
        // ignore: use_build_context_synchronously, unawaited_futures
        Routerino.context.pushRootImmediately(() => const HomePage(initialTab: HomeTab.send, appStart: false));
      }

      closeSession(sessionId);
      return;
    }

    final selectedFileIds = tarSupported
        ? (fileMap.isNotEmpty ? fileMap.keys.toSet() : requestState.files.keys.toSet())
        : fileMap.keys.toSet();
    final sendingFiles = {
      for (final file in requestState.files.values)
        file.file.id: selectedFileIds.contains(file.file.id)
            ? (tarSupported ? file.copyWith(status: FileStatus.queue) : file.copyWith(token: fileMap[file.file.id]))
            : file.copyWith(status: FileStatus.skipped),
    };

    if (state[sessionId]?.background == false) {
      final background = ref.read(settingsProvider).sendMode == SendMode.multiple;

      // ignore: use_build_context_synchronously, unawaited_futures
      Routerino.context.pushAndRemoveUntil(
        removeUntil: HomePage,
        transition: RouterinoTransition.fade(),
        // immediately is not possible: https://github.com/flutter/flutter/issues/121910
        builder: () => ProgressPage(
          showAppBar: background,
          closeSessionOnClose: !background,
          sessionId: sessionId,
        ),
      );
    }

    state = state.updateSession(
      sessionId: sessionId,
      state: (s) => s?.copyWith(
        status: SessionStatus.sending,
        files: sendingFiles,
      ),
    );

    if (tarSupported && tarToken != null) {
      await _sendTar(ref, sessionId, target, sendingFiles, tarToken);
      return;
    }

    await _sendLoop(ref, sessionId, target, sendingFiles);
  }

  Future<void> _sendLoop(Ref ref, String sessionId, Device target, Map<String, SendingFile> files) async {
    state = state.updateSession(
      sessionId: sessionId,
      state: (s) => s?.copyWith(startTime: DateTime.now().millisecondsSinceEpoch),
    );

    final queue = Queue<SendingFile>()..addAll(files.values);
    final concurrency = ref.read(parentIsolateProvider).uploadIsolateCount;
    _logger.info('Sending files using $concurrency concurrent isolates');

    final futures = List.generate(concurrency, (index) async {
      while (true) {
        final file = switch (queue.isEmpty) {
          true => null,
          false => queue.removeFirst(),
        };

        if (file == null) {
          break;
        }

        await sendFile(
          sessionId: sessionId,
          isolateIndex: index,
          file: file,
          isRetry: false,
        );
      }
    });

    await Future.wait(futures);

    _finish(sessionId: sessionId);
  }

  Future<void> _sendTar(
    Ref ref,
    String sessionId,
    Device target,
    Map<String, SendingFile> files,
    String tarToken,
  ) async {
    final selectedFiles = files.values.where((f) => f.status != FileStatus.skipped).toList();
    if (selectedFiles.isEmpty) {
      _finish(sessionId: sessionId);
      return;
    }

    state = state.updateSession(
      sessionId: sessionId,
      state: (s) => s?.copyWith(
        startTime: s.startTime ?? DateTime.now().millisecondsSinceEpoch,
        files: s.files.map((key, value) {
          if (value.status == FileStatus.skipped) {
            return MapEntry(key, value);
          }
          return MapEntry(key, value.copyWith(status: FileStatus.sending, errorMessage: null));
        }),
      ),
    );

    for (final file in selectedFiles) {
      ref.notifier(progressProvider).setProgress(sessionId: sessionId, fileId: file.file.id, progress: 0);
    }

    final layout = _buildTarLayout(selectedFiles);
    final tarStream = _createTarStream(selectedFiles);
    final securityContext = ref.read(securityProvider);
    final client = createRhttpClient(const Duration(days: 30), securityContext);
    final remoteSessionId = state[sessionId]?.remoteSessionId;
    if (remoteSessionId == null || remoteSessionId.isEmpty) {
      state = state.updateSession(
        sessionId: sessionId,
        state: (s) => s?.copyWith(
          status: SessionStatus.finishedWithErrors,
          errorMessage: 'Missing remote session id for TAR upload',
        ),
      );
      _finish(sessionId: sessionId);
      return;
    }

    final uri = ApiRoute.upload.target(
      target,
      query: {
        'sessionId': remoteSessionId,
        'token': tarToken,
        'tar': 'true',
      },
    );

    String? errorMessage;
    var currentIndex = 0;
    final cancelToken = CancelToken();
    _tarCancelTokens[sessionId] = cancelToken;
    try {
      await client.request(
        method: HttpMethod.post,
        expectBody: HttpExpectBody.bytes,
        url: uri,
        headers: HttpHeaders.rawMap({
          'Content-Length': layout.totalSize.toString(),
          'Content-Type': 'application/x-tar',
        }),
        body: HttpBody.stream(
          tarStream,
          length: layout.totalSize,
        ),
        onSendProgress: (curr, total) {
          final sentBytes = curr;
          while (currentIndex < layout.spans.length && sentBytes >= layout.spans[currentIndex].dataEnd) {
            final span = layout.spans[currentIndex];
            ref.notifier(progressProvider).setProgress(sessionId: sessionId, fileId: span.fileId, progress: 1);
            currentIndex++;
          }
          if (currentIndex < layout.spans.length) {
            final span = layout.spans[currentIndex];
            if (sentBytes > span.dataStart) {
              final raw = (sentBytes - span.dataStart) / span.size;
              final progress = raw.clamp(0, 1).toDouble();
              ref.notifier(progressProvider).setProgress(sessionId: sessionId, fileId: span.fileId, progress: progress);
            }
          }
        },
        cancelToken: cancelToken,
      );
    } catch (e, st) {
      errorMessage = e.humanErrorMessage;
      _logger.warning('Error while sending TAR stream', e, st);
    } finally {
      _tarCancelTokens.remove(sessionId);
    }

    state = state.updateSession(
      sessionId: sessionId,
      state: (s) => s?.copyWith(
        files: s.files.map((key, value) {
          if (value.status == FileStatus.skipped) {
            return MapEntry(key, value);
          }
          return MapEntry(
            key,
            value.copyWith(
              status: errorMessage != null ? FileStatus.failed : FileStatus.finished,
              errorMessage: errorMessage,
            ),
          );
        }),
      ),
    );

    if (errorMessage == null) {
      for (final file in selectedFiles) {
        ref.notifier(progressProvider).setProgress(sessionId: sessionId, fileId: file.file.id, progress: 1);
      }
    }

    _finish(sessionId: sessionId);
  }

  _TarLayout _buildTarLayout(List<SendingFile> files) {
    var offset = 0;
    final spans = <_TarFileSpan>[];
    for (final file in files) {
      final size = file.file.size;
      offset += _tarBlockSize; // header
      final dataStart = offset;
      final dataEnd = dataStart + size;
      offset = dataStart + _padTo512(size);
      spans.add(_TarFileSpan(fileId: file.file.id, dataStart: dataStart, dataEnd: dataEnd, size: size));
    }
    final totalSize = offset + (_tarBlockSize * 2); // end of archive
    return _TarLayout(totalSize: totalSize, spans: spans);
  }

  Stream<List<int>> _createTarStream(List<SendingFile> files) {
    final entries = Stream.fromIterable(files).asyncMap((file) async {
      final size = file.file.size;
      final modified = file.file.metadata?.lastModified ?? DateTime.now();
      final header = TarHeader(
        name: file.file.id,
        mode: int.parse('644', radix: 8),
        size: size,
        modified: modified,
      );
      return TarEntry(header, _openSendingFileStream(file));
    });
    return entries.transform(tarWriter);
  }

  Stream<List<int>> _openSendingFileStream(SendingFile file) {
    final path = file.path;
    if (path != null) {
      if (path.startsWith('content://')) {
        return uriContent.getContentStream(Uri.parse(path));
      }
      return io.File(path).openRead();
    }
    final bytes = file.bytes;
    if (bytes == null) {
      throw StateError('Missing file data for TAR entry: ${file.file.fileName}');
    }
    return Stream.fromIterable([bytes]);
  }

  int _padTo512(int size) {
    return ((size + _tarBlockSize - 1) ~/ _tarBlockSize) * _tarBlockSize;
  }

  void _finish({required String sessionId}) {
    final sessionState = state[sessionId];
    if (sessionState == null) {
      return;
    }

    if (state[sessionId]!.status != SessionStatus.sending) {
      _logger.info('Transfer was canceled.');
    } else {
      final hasError = sessionState.files.values.any((file) => file.status == FileStatus.failed);
      if (!hasError && sessionState.background == true) {
        // close session because everything is fine and it is in background
        closeSession(sessionId);
        _logger.info('Transfer finished and session removed.');
      } else {
        // keep session alive when there are errors or currently in foreground
        state = state.updateSession(
          sessionId: sessionId,
          state: (s) => s?.copyWith(
            status: hasError ? SessionStatus.finishedWithErrors : SessionStatus.finished,
            endTime: DateTime.now().millisecondsSinceEpoch,
          ),
        );

        if (hasError) {
          _logger.info('Transfer finished with errors.');
        } else {
          _logger.info('Transfer finished successfully.');
        }
      }
    }
  }

  final uriContent = UriContent();

  /// Sends a file.
  /// Returns true, if the next file should be sent.
  Future<bool> sendFile({
    required String sessionId,
    required int isolateIndex,
    required SendingFile file,
    required bool isRetry,
  }) async {
    final token = file.token;
    if (token == null) {
      return true;
    }

    final status = state[sessionId]?.status;
    const allowedStates = {SessionStatus.sending, SessionStatus.finishedWithErrors};
    if (status == null || !allowedStates.contains(status)) {
      return false;
    }

    final remoteSessionId = state[sessionId]!.remoteSessionId;
    final target = state[sessionId]!.target;

    if (isRetry) {
      _logger.info('Retrying ${file.file.fileName}');

      state = state.updateSession(
        sessionId: sessionId,
        state: (s) => s?.copyWith(
          status: SessionStatus.sending,
          files: s.files.map((key, value) {
            if (key == file.file.id) {
              return MapEntry(key, value.copyWith(status: FileStatus.queue, errorMessage: null));
            }
            return MapEntry(key, value);
          }),
        ),
      );
    } else {
      _logger.info('Sending ${file.file.fileName}');
    }

    state = state.updateSession(
      sessionId: sessionId,
      state: (s) => s?.withFileStatus(file.file.id, FileStatus.sending, null),
    );

    final taskResult = ref
        .redux(parentIsolateProvider)
        .dispatchTakeResult(
          IsolateHttpUploadAction(
            isolateIndex: isolateIndex,
            remoteSessionId: remoteSessionId,
            remoteFileToken: token,
            fileId: file.file.id,
            filePath: file.path,
            fileBytes: file.bytes,
            mime: file.file.lookupMime(),
            fileSize: file.file.size,
            device: target,
          ),
        );

    String? fileError;
    try {
      state = state.updateSession(
        sessionId: sessionId,
        state: (s) => s?.copyWith(
          sendingTasks: [
            ...?s.sendingTasks,
            SendingTask(
              isolateIndex: isolateIndex,
              taskId: taskResult.taskId,
            ),
          ],
        ),
      );

      await for (final progress in taskResult.progress) {
        ref
            .notifier(progressProvider)
            .setProgress(
              sessionId: sessionId,
              fileId: file.file.id,
              progress: progress,
            );
      }

      // set progress to 100% when successfully finished
      ref
          .notifier(progressProvider)
          .setProgress(
            sessionId: sessionId,
            fileId: file.file.id,
            progress: 1,
          );
    } catch (e, st) {
      fileError = e.humanErrorMessage;
      _logger.warning('Error while sending file ${file.file.fileName}', e, st);
    } finally {
      state = state.updateSession(
        sessionId: sessionId,
        state: (s) => s?.copyWith(
          sendingTasks: s.sendingTasks?.where((task) => !(task.isolateIndex == isolateIndex && task.taskId == taskResult.taskId)).toList(),
        ),
      );
    }

    state = state.updateSession(
      sessionId: sessionId,
      state: (s) => s?.withFileStatus(file.file.id, fileError != null ? FileStatus.failed : FileStatus.finished, fileError),
    );

    if (isRetry) {
      final state = this.state[sessionId];
      if (state != null && state.files.values.map((e) => e.status).isFinishedOrError) {
        _finish(sessionId: sessionId);
        return false;
      }
    }

    return true;
  }

  /// Closes the send-session and sends a cancel event to the receiver.
  void cancelSession(String sessionId) {
    final sessionState = state[sessionId];
    if (sessionState == null) {
      return;
    }
    final remoteSessionId = sessionState.remoteSessionId;

    _cancelRunningRequests(sessionState);

    if (remoteSessionId == null) {
      closeSession(sessionId);
      return;
    }

    // notify the receiver
    final target = sessionState.target;
    try {
      ref
          .read(httpProvider)
          .v2
          // ignore: discarded_futures
          .cancel(
            protocol: target.getProtocolType(),
            ip: target.ip!,
            port: target.port,
            sessionId: remoteSessionId,
          );
    } catch (e) {
      _logger.warning('Error while canceling session', e);
    }

    // finally, close session locally
    closeSession(sessionId);
  }

  void cancelSessionByReceiver(String sessionId) {
    final sessionState = state[sessionId];
    if (sessionState == null) {
      return;
    }
    _cancelRunningRequests(sessionState);

    state = state.updateSession(
      sessionId: sessionId,
      state: (s) => s?.copyWith(
        status: SessionStatus.canceledByReceiver,
        endTime: DateTime.now().millisecondsSinceEpoch,
      ),
    );
  }

  void _cancelRunningRequests(SendSessionState state) {
    final tarToken = _tarCancelTokens.remove(state.sessionId);
    tarToken?.cancel();
    for (final task in state.sendingTasks ?? <SendingTask>[]) {
      ref
          .redux(parentIsolateProvider)
          .dispatch(
            IsolateHttpUploadCancelAction(
              isolateIndex: task.isolateIndex,
              taskId: task.taskId,
            ),
          );
    }
  }

  /// Closes the session
  void closeSession(String sessionId) {
    final sessionState = state[sessionId];
    if (sessionState == null) {
      return;
    }
    state = state.removeSession(ref, sessionId);
    if (sessionState.status == SessionStatus.finished && ref.read(settingsProvider).sendMode == SendMode.single) {
      // clear selected files
      ref.redux(selectedSendingFilesProvider).dispatch(ClearSelectionAction());
    }
  }

  void clearAllSessions() {
    state = {};
    ref.notifier(progressProvider).removeAllSessions();
  }

  void setBackground(String sessionId, bool background) {
    state = state.updateSession(
      sessionId: sessionId,
      state: (s) => s?.copyWith(background: background),
    );
  }
}

class _TarLayout {
  final int totalSize;
  final List<_TarFileSpan> spans;

  const _TarLayout({
    required this.totalSize,
    required this.spans,
  });
}

class _TarFileSpan {
  final String fileId;
  final int dataStart;
  final int dataEnd;
  final int size;

  const _TarFileSpan({
    required this.fileId,
    required this.dataStart,
    required this.dataEnd,
    required this.size,
  });
}

extension on Map<String, SendSessionState> {
  Map<String, SendSessionState> updateSession({
    required String sessionId,
    required SendSessionState? Function(SendSessionState? old) state,
  }) {
    final newState = state(this[sessionId]);
    if (newState == null) {
      // no change
      return this;
    }
    return {
      ...this,
      sessionId: newState,
    };
  }

  Map<String, SendSessionState> removeSession(Ref ref, String sessionId) {
    ref.notifier(progressProvider).removeSession(sessionId);
    return {...this}..remove(sessionId);
  }
}

extension on SendSessionState {
  SendSessionState withFileStatus(String fileId, FileStatus status, String? errorMessage) {
    return copyWith(
      files: {...files}
        ..update(
          fileId,
          (file) => file.copyWith(
            status: status,
            errorMessage: errorMessage,
          ),
        ),
    );
  }
}

extension on Object {
  String get humanErrorMessage {
    final e = this;
    final (statusCode, message) = switch (this) {
      RhttpStatusCodeException(:final statusCode, :final body) => (statusCode, _parseErrorMessage(body)),
      _ => (null, e.toString()),
    };

    if (statusCode != null && message != null) {
      return '[$statusCode] $message';
    }

    return e.toString();
  }
}

String? _parseErrorMessage(Object? body) {
  if (body is! String) {
    return null;
  }

  try {
    return (jsonDecode(body) as Map)['message'];
  } catch (_) {
    return null;
  }
}
