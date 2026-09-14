from pathlib import Path


def replace_between(text: str, start: str, end: str, replacement: str) -> str:
    start_index = text.index(start)
    end_index = text.index(end, start_index)
    return text[:start_index] + replacement + text[end_index:]


sourced_path = Path("lib/services/sourced_track/sourced_track.dart")
sourced = sourced_path.read_text()

fetch_start = "  static Future<SourcedTrack> fetchFromTrack({\n"
fetch_end = "  static List<SpotubeAudioSourceMatchObject> rankResults(\n"
fetch_replacement = r'''  static Future<SourcedTrack> fetchFromTrack({
    required SpotubeFullTrackObject query,
    required Ref ref,
  }) async {
    final audioSource = await ref.read(audioSourcePluginProvider.future);
    final audioSourceConfig = await ref.read(metadataPluginsProvider
        .selectAsync((data) => data.defaultAudioSourcePluginConfig));
    if (audioSource == null || audioSourceConfig == null) {
      throw MetadataPluginException.noDefaultAudioSourcePlugin();
    }

    final database = ref.read(databaseProvider);

    Future<void> clearCachedSource() async {
      await (database.sourceMatchTable.delete()
            ..where(
              (table) =>
                  table.trackId.equals(query.id) &
                  table.sourceType.equals(audioSourceConfig.slug),
            ))
          .go();
    }

    Future<SourcedTrack> resolveFreshSource() async {
      final siblings = await fetchSiblings(ref: ref, query: query);
      if (siblings.isEmpty) {
        throw TrackNotFoundError(query);
      }

      Object? lastError;
      for (final candidate in siblings) {
        try {
          final manifest = await audioSource.audioSource.streams(candidate);
          if (manifest.isEmpty) {
            continue;
          }

          await clearCachedSource();
          await database.into(database.sourceMatchTable).insert(
                SourceMatchTableCompanion.insert(
                  trackId: query.id,
                  sourceInfo: Value(jsonEncode(candidate)),
                  sourceType: audioSourceConfig.slug,
                  createdAt: Value(DateTime.now()),
                ),
                mode: InsertMode.replace,
              );

          final sourcedTrack = SourcedTrack(
            ref: ref,
            siblings: siblings.where((item) => item.id != candidate.id).toList(),
            info: candidate,
            source: audioSourceConfig.slug,
            sources: manifest,
            query: query,
          );
          AppLogger.log.i("${query.name}: ${sourcedTrack.url}");
          return sourcedTrack;
        } catch (error) {
          lastError = error;
          AppLogger.log.w(
            "Failed audio source candidate ${candidate.id}; trying another: $error",
          );
        }
      }

      AppLogger.log.w("No playable source found for ${query.name}: $lastError");
      throw TrackNotFoundError(query);
    }

    final cachedSource = await (database.select(database.sourceMatchTable)
          ..where((s) =>
              s.trackId.equals(query.id) &
              s.sourceType.equals(audioSourceConfig.slug))
          ..limit(1)
          ..orderBy([
            (s) =>
                OrderingTerm(expression: s.createdAt, mode: OrderingMode.desc),
          ]))
        .get()
        .then((s) => s.firstOrNull);

    if (cachedSource == null) {
      return resolveFreshSource();
    }

    try {
      final item = SpotubeAudioSourceMatchObject.fromJson(
        jsonDecode(cachedSource.sourceInfo),
      );
      final manifest = await audioSource.audioSource.streams(item);
      if (manifest.isEmpty) {
        throw StateError("Cached audio source returned no streams");
      }

      final sourcedTrack = SourcedTrack(
        ref: ref,
        siblings: [],
        sources: manifest,
        info: item,
        query: query,
        source: audioSourceConfig.slug,
      );
      AppLogger.log.i("${query.name}: ${sourcedTrack.url}");
      return sourcedTrack;
    } catch (error) {
      AppLogger.log.w(
        "Cached audio source for ${query.name} is stale; refreshing: $error",
      );
      await clearCachedSource();
      return resolveFreshSource();
    }
  }

'''
sourced = replace_between(sourced, fetch_start, fetch_end, fetch_replacement)

siblings_start = "  static Future<List<SpotubeAudioSourceMatchObject>> fetchSiblings({\n"
siblings_end = "  Future<SourcedTrack> copyWithSibling() async {\n"
siblings_replacement = r'''  static Future<List<SpotubeAudioSourceMatchObject>> fetchSiblings({
    required SpotubeFullTrackObject query,
    required Ref ref,
  }) async {
    final audioSource = await ref.read(audioSourcePluginProvider.future);

    if (audioSource == null) {
      throw MetadataPluginException.noDefaultAudioSourcePlugin();
    }

    var searchResults = await audioSource.audioSource.matches(query);

    if (searchResults.isEmpty) {
      try {
        AppLogger.log.i("Audio source returned no matches; retrying authentication");
        await audioSource.auth.authenticate();
        searchResults = await audioSource.audioSource.matches(query);
      } catch (error) {
        AppLogger.log.w("Audio source authentication retry failed: $error");
      }
    }

    final videoResults = <SpotubeAudioSourceMatchObject>[];
    if (ServiceUtils.onlyContainsEnglish(query.name)) {
      videoResults.addAll(searchResults);
    } else {
      videoResults.addAll(rankResults(searchResults, query));
    }

    return videoResults.toSet().toList();
  }

'''
sourced = replace_between(sourced, siblings_start, siblings_end, siblings_replacement)

quality_start = "  String? get url {\n"
quality_end = "  SpotubeAudioSourceContainerPreset? get qualityPreset {\n"
quality_replacement = r'''  String? get url {
    if (sources.isEmpty) return null;

    final preferences = ref.read(audioSourcePresetsProvider);
    final preset = preferences.presets.elementAtOrNull(
      preferences.selectedStreamingContainerIndex,
    );

    if (preset == null) {
      return sources.first.url;
    }

    return getUrlOfQuality(
          preset,
          preferences.selectedStreamingQualityIndex,
        ) ??
        sources.first.url;
  }

  /// Returns the closest stream for the selected preset. If the configured
  /// preset no longer exists or the source does not expose that container,
  /// fall back to the first stream instead of leaving playback stuck at 0:00.
  SpotubeAudioSourceStreamObject? getStreamOfQuality(
    SpotubeAudioSourceContainerPreset preset,
    int qualityIndex,
  ) {
    if (sources.isEmpty) return null;

    final quality = preset.qualities.elementAtOrNull(qualityIndex);
    if (quality == null) return sources.first;

    final matchingSources = sources
        .where((source) => source.container == preset.name)
        .toList();
    if (matchingSources.isEmpty) return sources.first;

    final exactMatch = matchingSources.firstWhereOrNull(
      (source) {
        if (quality case SpotubeAudioLosslessContainerQuality()) {
          return source.sampleRate == quality.sampleRate &&
              source.bitDepth == quality.bitDepth;
        } else {
          return source.bitrate ==
              (quality as SpotubeAudioLossyContainerQuality).bitrate;
        }
      },
    );

    if (exactMatch != null) return exactMatch;

    return matchingSources.reduce((prev, curr) {
      if (quality is SpotubeAudioLosslessContainerQuality) {
        final prevDiff = ((prev.sampleRate ?? 0) - quality.sampleRate).abs() +
            ((prev.bitDepth ?? 0) - quality.bitDepth).abs();
        final currDiff = ((curr.sampleRate ?? 0) - quality.sampleRate).abs() +
            ((curr.bitDepth ?? 0) - quality.bitDepth).abs();
        return currDiff < prevDiff ? curr : prev;
      } else {
        final prevDiff = ((prev.bitrate ?? 0) -
                (quality as SpotubeAudioLossyContainerQuality).bitrate)
            .abs();
        final currDiff = ((curr.bitrate ?? 0) - quality.bitrate).abs();
        return currDiff < prevDiff ? curr : prev;
      }
    });
  }

  String? getUrlOfQuality(
    SpotubeAudioSourceContainerPreset preset,
    int qualityIndex,
  ) {
    return getStreamOfQuality(preset, qualityIndex)?.url ??
        sources.firstOrNull?.url;
  }

'''
sourced = replace_between(sourced, quality_start, quality_end, quality_replacement)
sourced_path.write_text(sourced)

metadata_path = Path("lib/services/metadata/metadata.dart")
metadata = metadata_path.read_text()
old_duration = "'duration': video.duration?.inSeconds,"
if metadata.count(old_duration) != 2:
    raise RuntimeError(
        f"Expected two YouTube duration mappings, found {metadata.count(old_duration)}"
    )
metadata = metadata.replace(
    old_duration,
    "'duration': video.duration?.inMicroseconds,",
)
metadata_path.write_text(metadata)

gradle_path = Path("android/app/build.gradle")
gradle = gradle_path.read_text()
debug_release_signing = '''        debug {
            signingConfig signingConfigs.release
        }
'''
if debug_release_signing not in gradle:
    raise RuntimeError("Expected debug release-signing block was not found")
gradle = gradle.replace(debug_release_signing, "", 1)

stable_flavor = '''        stable {
            dimension "default"
            resValue "string", "app_name_en", "Spotube"
            signingConfig signingConfigs.release
        }
'''
fixed_flavor = stable_flavor + '''        fixed {
            dimension "default"
            resValue "string", "app_name_en", "Spotube Fix"
            applicationIdSuffix ".fixed"
            versionNameSuffix "-fixed"
        }
'''
if stable_flavor not in gradle:
    raise RuntimeError("Expected stable flavor block was not found")
gradle = gradle.replace(stable_flavor, fixed_flavor, 1)
gradle_path.write_text(gradle)

print("Applied Spotube Android playback fixes:")
print("- stale audio-source cache invalidation and sibling fallback")
print("- authentication retry when an audio source returns no matches")
print("- quality/container fallback to the first available stream")
print("- YouTube duration mapping changed to microseconds")
print("- added separate oss.krtirtho.spotube.fixed / Spotube Fix flavor")
