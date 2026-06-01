import 'package:dart_mappable/dart_mappable.dart';

part 'prepare_upload_response_dto.mapper.dart';

@MappableClass()
class PrepareUploadResponseDto with PrepareUploadResponseDtoMappable {
  final String sessionId;
  final Map<String, String> files;
  final bool? tarSupported;
  final String? tarToken;

  const PrepareUploadResponseDto({
    required this.sessionId,
    required this.files,
    this.tarSupported,
    this.tarToken,
  });

  static const fromJson = PrepareUploadResponseDtoMapper.fromJson;
}
