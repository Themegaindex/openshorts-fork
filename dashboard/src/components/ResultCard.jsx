import React, { useState, useEffect } from 'react';
import { Download, Share2, Instagram, Youtube, Video, CheckCircle, AlertCircle, X, Loader2, Copy, Wand2, Type, Calendar, Clock, Languages } from 'lucide-react';
import { getApiUrl } from '../config';
import { readApiError } from '../apiError';
import SubtitleModal from './SubtitleModal';
import HookModal from './HookModal';
import TranslateModal from './TranslateModal';

export default function ResultCard({ clip, index, jobId, uploadPostKey, uploadUserId, geminiApiKey, elevenLabsKey, onVersionChange, onPlay, onPause }) {
    const isLong = clip.video_type === 'long';
    const [showModal, setShowModal] = useState(false);
    const [showSubtitleModal, setShowSubtitleModal] = useState(false);
    const videoRef = React.useRef(null);
    const [currentVideoUrl, setCurrentVideoUrl] = useState(getApiUrl(clip.video_url));
    const [videoError, setVideoError] = useState(null);

    const [platforms, setPlatforms] = useState({
        tiktok: true,
        instagram: true,
        youtube: true
    });
    const [postTitle, setPostTitle] = useState("");
    const [postDescription, setPostDescription] = useState("");
    const [isScheduling, setIsScheduling] = useState(false);
    const [scheduleDate, setScheduleDate] = useState("");

    const [posting, setPosting] = useState(false);
    const [postResult, setPostResult] = useState(null);

    const [isEditing, setIsEditing] = useState(false);
    const [isSubtitling, setIsSubtitling] = useState(false);
    const [isHooking, setIsHooking] = useState(false);
    const [isTranslating, setIsTranslating] = useState(false);
    const [showHookModal, setShowHookModal] = useState(false);
    const [showTranslateModal, setShowTranslateModal] = useState(false);
    const [editError, setEditError] = useState(null);
    // Which burned-in layers this clip currently carries. The server reports
    // them so the remove buttons survive a page reload.
    const [layers, setLayers] = useState(clip.layers || { subtitle: false, hook: false });
    const [removingLayer, setRemovingLayer] = useState(null);
    const [copiedField, setCopiedField] = useState(null);

    useEffect(() => {
        if (clip.layers) setLayers(clip.layers);
    }, [clip.layers]);

    // Initialize/Reset form when modal opens
    useEffect(() => {
        if (showModal) {
            setPostTitle(clip.video_title_for_youtube_short || "Viral Short");
            setPostDescription(clip.video_description_for_instagram || clip.video_description_for_tiktok || "");
            setIsScheduling(false);
            setScheduleDate("");
            setPostResult(null);
        }
    }, [showModal, clip]);

    const handleAutoEdit = async () => {
        setIsEditing(true);
        setEditError(null);
        try {
            // Use passed prop or fallback
            const apiKey = geminiApiKey || localStorage.getItem('gemini_key');

            if (!apiKey) {
                throw new Error("Gemini API Key is missing. Please set it in Settings.");
            }

            const res = await fetch(getApiUrl('/api/edit'), {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Gemini-Key': apiKey
                },
                body: JSON.stringify({
                    job_id: jobId,
                    clip_index: index,
                    input_filename: currentVideoUrl.split('/').pop()
                })
            });

            if (!res.ok) throw new Error(await readApiError(res));

            const data = await res.json();
            if (data.new_video_url) {
                setCurrentVideoUrl(getApiUrl(data.new_video_url));
                // Auto Edit keeps the layers; pass them so the remount does
                // not fall back to a stale copy from the parent.
                onVersionChange?.(index, data.new_video_url, layers);
                // Reload video
                if (videoRef.current) {
                    videoRef.current.load();
                }
            }

        } catch (e) {
            setEditError(e.message);
            setTimeout(() => setEditError(null), 5000);
        } finally {
            setIsEditing(false);
        }
    };

    const handleSubtitle = async (options) => {
        setIsSubtitling(true);
        setEditError(null);
        try {
            const res = await fetch(getApiUrl('/api/subtitle'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    job_id: jobId,
                    clip_index: index,
                    position: options.position,
                    font_size: options.fontSize,
                    font_name: options.fontName,
                    font_color: options.fontColor,
                    border_color: options.borderColor,
                    border_width: options.borderWidth,
                    bg_color: options.bgColor,
                    bg_opacity: options.bgOpacity,
                    style: options.style,
                    preset: options.preset,
                    highlight_color: options.highlightColor,
                    effect: options.effect,
                    base_opacity: options.baseOpacity,
                    uppercase: options.uppercase,
                    input_filename: currentVideoUrl.split('/').pop()
                })
            });

            if (!res.ok) throw new Error(await readApiError(res));

            const data = await res.json();
            if (data.new_video_url) {
                const nextLayers = { ...layers, subtitle: true };
                setLayers(nextLayers);
                setCurrentVideoUrl(getApiUrl(data.new_video_url));
                onVersionChange?.(index, data.new_video_url, nextLayers);
                if (videoRef.current) {
                    videoRef.current.load();
                }
                setShowSubtitleModal(false);
            }

        } catch (e) {
            setEditError(e.message);
            setTimeout(() => setEditError(null), 5000);
        } finally {
            setIsSubtitling(false);
        }
    };

    const handleHook = async (hookData) => {
        setIsHooking(true);
        setEditError(null);
        try {
            // Support both string (legacy) and object
            const payload = typeof hookData === 'string'
                ? { text: hookData, position: 'top', size: 'M' }
                : hookData;

            const res = await fetch(getApiUrl('/api/hook'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    job_id: jobId,
                    clip_index: index,
                    text: payload.text,
                    position: payload.position,
                    size: payload.size,
                    input_filename: currentVideoUrl.split('/').pop()
                })
            });

            if (!res.ok) throw new Error(await readApiError(res));

            const data = await res.json();
            if (data.new_video_url) {
                const nextLayers = { ...layers, hook: true };
                setLayers(nextLayers);
                setCurrentVideoUrl(getApiUrl(data.new_video_url));
                onVersionChange?.(index, data.new_video_url, nextLayers);
                if (videoRef.current) {
                    videoRef.current.load();
                }
                setShowHookModal(false);
            }

        } catch (e) {
            setEditError(e.message);
            setTimeout(() => setEditError(null), 5000);
        } finally {
            setIsHooking(false);
        }
    };

    const handleTranslate = async (options) => {
        console.log('[Translate] Starting translation with options:', options);
        setIsTranslating(true);
        setEditError(null);
        try {
            const apiKey = elevenLabsKey;
            console.log('[Translate] API Key available:', !!apiKey);

            if (!apiKey) {
                throw new Error("ElevenLabs API Key is missing. Please set it in Settings.");
            }

            const requestBody = {
                job_id: jobId,
                clip_index: index,
                target_language: options.targetLanguage,
                input_filename: currentVideoUrl.split('/').pop()
            };
            console.log('[Translate] Request body:', requestBody);
            console.log('[Translate] Sending request to /api/translate');

            const res = await fetch(getApiUrl('/api/translate'), {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-ElevenLabs-Key': apiKey
                },
                body: JSON.stringify(requestBody)
            });

            console.log('[Translate] Response status:', res.status);

            if (!res.ok) {
                const message = await readApiError(res);
                console.error('[Translate] Error response:', message);
                throw new Error(message);
            }

            const data = await res.json();
            console.log('[Translate] Success response:', data);
            if (data.new_video_url) {
                // Dubbing drops the now-stale subtitles server-side.
                const nextLayers = { ...layers, subtitle: false };
                setLayers(nextLayers);
                setCurrentVideoUrl(getApiUrl(data.new_video_url));
                onVersionChange?.(index, data.new_video_url, nextLayers);
                if (videoRef.current) {
                    videoRef.current.load();
                }
                setShowTranslateModal(false);
            }

        } catch (e) {
            console.error('[Translate] Exception:', e);
            setEditError(e.message);
            setTimeout(() => setEditError(null), 5000);
        } finally {
            setIsTranslating(false);
        }
    };

    const handleRemoveLayer = async (layer) => {
        setRemovingLayer(layer);
        setEditError(null);
        try {
            const res = await fetch(getApiUrl('/api/clip/remove-layer'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ job_id: jobId, clip_index: index, layer })
            });
            if (!res.ok) throw new Error(await readApiError(res));

            const data = await res.json();
            const nextLayers = data.layers || layers;
            setLayers(nextLayers);
            if (data.new_video_url) {
                setCurrentVideoUrl(getApiUrl(data.new_video_url));
                onVersionChange?.(index, data.new_video_url, nextLayers);
                if (videoRef.current) videoRef.current.load();
            }
        } catch (e) {
            setEditError(e.message);
            setTimeout(() => setEditError(null), 5000);
        } finally {
            setRemovingLayer(null);
        }
    };

    const handlePost = async () => {
        if (!uploadPostKey || !uploadUserId) {
            setPostResult({ success: false, msg: "Missing API Key or User ID." });
            return;
        }

        const selectedPlatforms = Object.keys(platforms).filter(k => platforms[k]);
        if (selectedPlatforms.length === 0) {
            setPostResult({ success: false, msg: "Select at least one platform." });
            return;
        }

        if (isScheduling && !scheduleDate) {
            setPostResult({ success: false, msg: "Please select a date and time." });
            return;
        }

        setPosting(true);
        setPostResult(null);

        try {
            const payload = {
                job_id: jobId,
                clip_index: index,
                api_key: uploadPostKey,
                user_id: uploadUserId,
                platforms: selectedPlatforms,
                title: postTitle,
                description: postDescription
            };

            if (isScheduling && scheduleDate) {
                // Convert to ISO-8601
                payload.scheduled_date = new Date(scheduleDate).toISOString();
                // Optional: pass timezone if needed, backend defaults to UTC or we can send user's timezone
                payload.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
            }

            const res = await fetch(getApiUrl('/api/social/post'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            if (!res.ok) throw new Error(await readApiError(res));

            setPostResult({ success: true, msg: isScheduling ? "Scheduled successfully!" : "Posted successfully!" });
            setTimeout(() => {
                setShowModal(false);
                setPostResult(null);
            }, 3000);

        } catch (e) {
            setPostResult({ success: false, msg: `Failed: ${e.message}` });
        } finally {
            setPosting(false);
        }
    };

    const copyText = async (field, value) => {
        if (!value) return;
        try {
            await navigator.clipboard.writeText(value);
            setCopiedField(field);
            setTimeout(() => setCopiedField(null), 1600);
        } catch (error) {
            console.error('Copy failed:', error);
        }
    };

    const handleDownload = (event) => {
        event.preventDefault();
        const downloadUrl = jobId
            ? getApiUrl(`/api/jobs/${encodeURIComponent(jobId)}/clips/${index}/download`)
            : currentVideoUrl;
        const anchor = document.createElement('a');
        anchor.style.display = 'none';
        anchor.href = downloadUrl;
        anchor.download = isLong ? 'long-video.mp4' : `clip-${index + 1}.mp4`;
        document.body.appendChild(anchor);
        anchor.click();
        document.body.removeChild(anchor);
    };

    const handleVideoError = () => {
        const code = videoRef.current?.error?.code;
        const messages = {
            2: 'The preview could not reach the video file.',
            3: 'The browser could not decode this preview.',
            4: 'This browser does not support the generated video.',
        };
        setVideoError(messages[code] || 'The video preview could not start.');
    };

    const retryVideo = () => {
        setVideoError(null);
        const video = videoRef.current;
        if (!video) return;
        video.load();
        video.play().catch(() => {
            // The browser may still require the native play button. A real
            // media error will fire onError and restore the message.
        });
    };

    return (
        <div className={`bg-surface border border-white/5 rounded-2xl overflow-hidden flex group hover:border-white/10 transition-all animate-[fadeIn_0.5s_ease-out] h-auto ${isLong ? 'xl:col-span-2 flex-col min-h-[520px]' : 'flex-col md:flex-row min-h-[300px]'}`} style={{ animationDelay: `${index * 0.1}s` }}>
            {/* Left: Video Preview (Responsive Width) */}
            <div className={`${isLong ? 'w-full aspect-video border-b border-white/5' : 'w-full md:w-[180px] lg:w-[200px] aspect-[9/16] md:aspect-auto'} bg-black relative shrink-0 group/video`}>
                <video
                    ref={videoRef}
                    src={currentVideoUrl}
                    controls
                    preload="metadata"
                    className="w-full h-full object-contain"
                    playsInline
                    onCanPlay={() => setVideoError(null)}
                    onError={handleVideoError}
                    onPlay={() => {
                        // A long edit has a discontinuous assembled timeline,
                        // so its playback time cannot seek the original preview.
                        if (isLong) return;
                        const currentTime = videoRef.current ? videoRef.current.currentTime : 0;
                        onPlay && onPlay(clip.start + currentTime);
                    }}
                    onPause={() => !isLong && onPause && onPause()}
                    onEnded={() => {
                        if (videoRef.current) {
                            videoRef.current.currentTime = 0;
                            videoRef.current.play();
                        }
                    }}
                />
                {videoError && (
                    <div className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-3 bg-black/80 p-5 text-center">
                        <AlertCircle size={24} className="text-amber-400" />
                        <p className="max-w-sm text-xs text-zinc-200">{videoError}</p>
                        <button
                            type="button"
                            onClick={retryVideo}
                            className="rounded-lg border border-white/15 bg-white/10 px-3 py-2 text-xs font-semibold text-white transition-colors hover:bg-white/15"
                        >
                            Reload preview
                        </button>
                        <span className="text-[10px] text-zinc-500">The download remains available.</span>
                    </div>
                )}
                <div className="absolute top-3 left-3 flex gap-2">
                    <span className="bg-black/60 backdrop-blur-md text-white text-[10px] font-bold px-2 py-1 rounded-md border border-white/10 uppercase tracking-wide">
                        {isLong ? 'Long Video · 16:9' : `Clip ${index + 1}`}
                    </span>
                </div>

                {/* Auto Edit Overlay if Processing */}
                {!isLong && isEditing && (
                    <div className="absolute inset-0 bg-black/60 backdrop-blur-sm flex flex-col items-center justify-center z-10 p-4 text-center">
                        <Loader2 size={32} className="text-primary animate-spin mb-3" />
                        <span className="text-xs font-bold text-white uppercase tracking-wider">AI Magic in Progress...</span>
                        <span className="text-[10px] text-zinc-400 mt-1">Applying viral edits & zooms</span>
                    </div>
                )}
            </div>

            {/* Right: Content & Details */}
            <div className={`flex-1 flex flex-col bg-[#121214] overflow-hidden min-w-0 ${isLong ? 'p-5 md:p-7' : 'p-4 md:p-5'}`}>
                <div className="mb-4">
                    <div className="flex items-start gap-3">
                        <h3 className={`${isLong ? 'text-xl md:text-2xl max-w-3xl' : 'text-base line-clamp-2'} flex-1 font-bold text-white leading-tight mb-2 break-words`} title={isLong ? clip.title : clip.video_title_for_youtube_short}>
                            {isLong ? (clip.title || "Long Video Generated") : (clip.video_title_for_youtube_short || "Viral Clip Generated")}
                        </h3>
                        {isLong && (
                            <button
                                type="button"
                                onClick={() => copyText('title', clip.title)}
                                className="mt-0.5 rounded-lg border border-white/10 bg-white/5 p-2 text-zinc-400 transition-colors hover:bg-white/10 hover:text-white"
                                title="Copy title"
                            >
                                {copiedField === 'title' ? <CheckCircle size={14} className="text-emerald-400" /> : <Copy size={14} />}
                            </button>
                        )}
                    </div>
                    <div className="flex flex-wrap gap-2 text-[10px] text-zinc-500 font-mono">
                        <span className="bg-white/5 px-1.5 py-0.5 rounded border border-white/5 shrink-0">{Math.floor(clip.duration ?? (clip.end - clip.start))}s</span>
                        {isLong ? (
                            <>
                                <span className="bg-sky-500/10 px-1.5 py-0.5 rounded border border-sky-500/20 text-sky-300 shrink-0">16:9 canvas</span>
                                <span className="bg-white/5 px-1.5 py-0.5 rounded border border-white/5 shrink-0">YouTube-ready</span>
                            </>
                        ) : (
                            <>
                                <span className="bg-white/5 px-1.5 py-0.5 rounded border border-white/5 shrink-0">#shorts</span>
                                <span className="bg-white/5 px-1.5 py-0.5 rounded border border-white/5 shrink-0">#viral</span>
                            </>
                        )}
                    </div>
                </div>

                {/* Scrollable Descriptions Area */}
                {isLong ? (
                    <div className="mb-5 grid flex-1 gap-px overflow-hidden rounded-xl border border-white/5 bg-white/5 md:grid-cols-[1.35fr_0.65fr]">
                        <div className="min-w-0 bg-black/30 p-4 md:p-5">
                            <div className="mb-3 flex items-center justify-between gap-3">
                                <div className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.16em] text-red-400">
                                    <Youtube size={13} /> Description + chapters
                                </div>
                                <button
                                    type="button"
                                    onClick={() => copyText('description', clip.description_with_chapters)}
                                    className="flex items-center gap-1.5 rounded-md border border-white/10 px-2 py-1 text-[10px] text-zinc-400 transition-colors hover:bg-white/10 hover:text-white"
                                >
                                    {copiedField === 'description' ? <CheckCircle size={11} className="text-emerald-400" /> : <Copy size={11} />}
                                    {copiedField === 'description' ? 'Copied' : 'Copy'}
                                </button>
                            </div>
                            <p className="whitespace-pre-wrap text-xs leading-relaxed text-zinc-300 select-all break-words">
                                {clip.description_with_chapters || clip.youtube_description || "No description generated."}
                            </p>
                        </div>
                        <div className="bg-[#101012] p-4 md:p-5">
                            <div className="mb-3 text-[10px] font-bold uppercase tracking-[0.16em] text-zinc-500">Timeline</div>
                            <ol className="max-h-48 space-y-2 overflow-y-auto pr-1 font-mono text-[10px] custom-scrollbar">
                                {(clip.chapters || []).map((chapter, chapterIndex) => (
                                    <li key={`${chapter.formatted}-${chapterIndex}`} className="grid grid-cols-[42px_1fr] gap-2 border-b border-white/5 pb-2 last:border-0">
                                        <span className="text-sky-400">{chapter.formatted}</span>
                                        <span className="text-zinc-300">{chapter.title}</span>
                                    </li>
                                ))}
                            </ol>
                        </div>
                    </div>
                ) : (
                    <div className="flex-1 overflow-y-auto custom-scrollbar space-y-3 pr-2 mb-4">
                        <div className="bg-black/20 rounded-lg p-3 border border-white/5">
                            <div className="flex items-center gap-2 text-[10px] font-bold text-red-400 mb-1.5 uppercase tracking-wider">
                                <Youtube size={12} className="shrink-0" /> <span className="truncate">YouTube Title</span>
                            </div>
                            <p className="text-xs text-zinc-300 select-all break-words">
                                {clip.video_title_for_youtube_short || "Viral Short Video"}
                            </p>
                        </div>

                        <div className="bg-black/20 rounded-lg p-3 border border-white/5">
                            <div className="flex items-center gap-2 text-[10px] font-bold text-zinc-400 mb-1.5 uppercase tracking-wider">
                                <Video size={12} className="text-cyan-400 shrink-0" />
                                <span className="text-zinc-500">/</span>
                                <Instagram size={12} className="text-pink-400 shrink-0" />
                                <span className="truncate">Caption</span>
                            </div>
                            <p className="text-xs text-zinc-300 line-clamp-3 hover:line-clamp-none transition-all cursor-pointer select-all break-words">
                                {clip.video_description_for_tiktok || clip.video_description_for_instagram}
                            </p>
                        </div>
                    </div>
                )}

                {/* Error Message */}
                {editError && (
                    <div className="mb-3 p-2 bg-red-500/10 border border-red-500/20 text-red-400 text-[10px] rounded-lg flex items-center gap-2">
                        <AlertCircle size={12} className="shrink-0" />
                        {editError}
                    </div>
                )}

                {/* Applied layers. Burning one in used to be a one-way door:
                    the only way out was overwriting it with another style. */}
                {(layers.subtitle || (!isLong && layers.hook)) && (
                    <div className="mt-auto flex flex-wrap items-center gap-2 pt-4 text-[10px]">
                        <span className="text-zinc-600 uppercase tracking-wider">Applied</span>
                        {[
                            { id: 'subtitle', label: 'Subtitles', active: layers.subtitle },
                            { id: 'hook', label: 'Hook', active: !isLong && layers.hook },
                        ].filter((entry) => entry.active).map((entry) => (
                            <button
                                key={entry.id}
                                onClick={() => handleRemoveLayer(entry.id)}
                                disabled={removingLayer !== null}
                                title={`Remove ${entry.label.toLowerCase()} from this clip`}
                                className="group flex items-center gap-1 rounded-full border border-white/10 bg-white/5 px-2 py-0.5 text-zinc-300 transition-colors hover:border-red-500/40 hover:bg-red-500/10 hover:text-red-300 disabled:opacity-50"
                            >
                                {removingLayer === entry.id
                                    ? <Loader2 size={10} className="animate-spin" />
                                    : <X size={10} className="text-zinc-500 group-hover:text-red-300" />}
                                {entry.label}
                            </button>
                        ))}
                    </div>
                )}

                {/* Actions Footer */}
                {isLong ? (
                    <div className="mt-auto grid grid-cols-2 gap-3 border-t border-white/5 pt-4">
                        <button
                            onClick={() => setShowSubtitleModal(true)}
                            disabled={isSubtitling}
                            className="flex items-center justify-center gap-2 rounded-lg bg-gradient-to-r from-yellow-600 to-orange-600 px-3 py-2.5 text-xs font-bold text-white shadow-lg shadow-orange-500/20 transition-all hover:from-yellow-500 hover:to-orange-500 active:scale-[0.98] disabled:opacity-60"
                        >
                            {isSubtitling ? <Loader2 size={14} className="animate-spin" /> : <Type size={14} />}
                            {isSubtitling ? 'Adding...' : 'Subtitles'}
                        </button>
                        <button
                            onClick={handleDownload}
                            className="flex items-center justify-center gap-2 rounded-lg border border-white/10 bg-white/5 px-3 py-2.5 text-xs font-medium text-zinc-300 transition-colors hover:bg-white/10 hover:text-white"
                        >
                            <Download size={14} /> Download Long Video
                        </button>
                    </div>
                ) : (
                <div className={`grid grid-cols-2 gap-3 ${layers.subtitle || layers.hook ? 'mt-3' : 'mt-auto'} pt-4 border-t border-white/5`}>
                    <button
                        onClick={handleAutoEdit}
                        disabled={isEditing}
                        className="col-span-1 py-2 bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-500 hover:to-indigo-500 text-white rounded-lg text-xs font-bold shadow-lg shadow-purple-500/20 transition-all active:scale-[0.98] flex items-center justify-center gap-2 mb-1 truncate px-1"
                    >
                        {isEditing ? <Loader2 size={14} className="animate-spin" /> : <Wand2 size={14} />}
                        {isEditing ? 'Editing...' : 'Auto Edit'}
                    </button>

                    <button
                        onClick={() => setShowSubtitleModal(true)}
                        disabled={isSubtitling}
                        className="col-span-1 py-2 bg-gradient-to-r from-yellow-600 to-orange-600 hover:from-yellow-500 hover:to-orange-500 text-white rounded-lg text-xs font-bold shadow-lg shadow-orange-500/20 transition-all active:scale-[0.98] flex items-center justify-center gap-2 mb-1 truncate px-1"
                    >
                        {isSubtitling ? <Loader2 size={14} className="animate-spin" /> : <Type size={14} />}
                        {isSubtitling ? 'Adding...' : 'Subtitles'}
                    </button>

                    <button
                        onClick={() => setShowHookModal(true)}
                        disabled={isHooking}
                        className="col-span-1 py-2 bg-gradient-to-r from-amber-400 to-yellow-500 hover:from-amber-300 hover:to-yellow-400 text-black rounded-lg text-xs font-bold shadow-lg shadow-yellow-500/20 transition-all active:scale-[0.98] flex items-center justify-center gap-2 mb-1 truncate px-1"
                    >
                        {isHooking ? <Loader2 size={14} className="animate-spin" /> : <Wand2 size={14} />}
                        {isHooking ? 'Adding...' : 'Viral Hook'}
                    </button>

                    <button
                        onClick={() => setShowTranslateModal(true)}
                        disabled={isTranslating}
                        className="col-span-1 py-2 bg-gradient-to-r from-green-500 to-teal-600 hover:from-green-400 hover:to-teal-500 text-white rounded-lg text-xs font-bold shadow-lg shadow-green-500/20 transition-all active:scale-[0.98] flex items-center justify-center gap-2 mb-1 truncate px-1"
                    >
                        {isTranslating ? <Loader2 size={14} className="animate-spin" /> : <Languages size={14} />}
                        {isTranslating ? 'Translating...' : 'Dub Voice'}
                    </button>

                    <button
                        onClick={() => setShowModal(true)}
                        className="col-span-1 py-2 bg-primary hover:bg-blue-600 text-white rounded-lg text-xs font-bold shadow-lg shadow-primary/20 transition-all active:scale-[0.98] flex items-center justify-center gap-2 truncate px-2"
                    >
                        <Share2 size={14} className="shrink-0" /> Post
                    </button>
                    <button
                        onClick={handleDownload}
                        className="col-span-1 py-2 bg-white/5 hover:bg-white/10 text-zinc-300 hover:text-white rounded-lg text-xs font-medium transition-colors flex items-center justify-center gap-2 border border-white/5 truncate px-2"
                    >
                        <Download size={14} className="shrink-0" /> Download
                    </button>
                </div>
                )}
            </div>

            {/* Post Modal */}
            {!isLong && showModal && (
                <div className="fixed inset-0 z-[100] flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm animate-[fadeIn_0.2s_ease-out]">
                    <div className="bg-[#121214] border border-white/10 p-6 rounded-2xl w-full max-w-md shadow-2xl relative max-h-[90vh] overflow-y-auto custom-scrollbar">
                        <button
                            onClick={() => setShowModal(false)}
                            className="absolute top-4 right-4 text-zinc-500 hover:text-white"
                        >
                            <X size={20} />
                        </button>

                        <h3 className="text-lg font-bold text-white mb-4">Post / Schedule</h3>

                        {!uploadPostKey && (
                            <div className="mb-4 p-3 bg-yellow-500/10 border border-yellow-500/20 text-yellow-200 text-xs rounded-lg flex items-start gap-2">
                                <AlertCircle size={14} className="mt-0.5 shrink-0" />
                                <div>Configure API Key in Settings first.</div>
                            </div>
                        )}

                        <div className="space-y-4 mb-6">
                            {/* Title & Description */}
                            <div>
                                <label className="block text-xs font-bold text-zinc-400 mb-1">Video Title</label>
                                <input
                                    type="text"
                                    value={postTitle}
                                    onChange={(e) => setPostTitle(e.target.value)}
                                    className="w-full bg-black/40 border border-white/10 rounded-lg p-2 text-sm text-white focus:outline-none focus:border-primary/50 placeholder-zinc-600"
                                    placeholder="Enter a catchy title..."
                                />
                            </div>

                            <div>
                                <label className="block text-xs font-bold text-zinc-400 mb-1">Caption / Description</label>
                                <textarea
                                    value={postDescription}
                                    onChange={(e) => setPostDescription(e.target.value)}
                                    rows={4}
                                    className="w-full bg-black/40 border border-white/10 rounded-lg p-2 text-sm text-white focus:outline-none focus:border-primary/50 placeholder-zinc-600 resize-none"
                                    placeholder="Write a caption for your post..."
                                />
                            </div>

                            {/* Scheduling */}
                            <div className="p-3 bg-white/5 rounded-lg border border-white/5">
                                <div className="flex items-center justify-between mb-2">
                                    <div className="flex items-center gap-2 text-sm text-white font-medium">
                                        <Calendar size={16} className="text-purple-400" /> Schedule Post
                                    </div>
                                    <label className="relative inline-flex items-center cursor-pointer">
                                        <input type="checkbox" checked={isScheduling} onChange={(e) => setIsScheduling(e.target.checked)} className="sr-only peer" />
                                        <div className="w-9 h-5 bg-zinc-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-purple-600"></div>
                                    </label>
                                </div>

                                {isScheduling && (
                                    <div className="mt-3 animate-[fadeIn_0.2s_ease-out]">
                                        <label className="block text-xs text-zinc-400 mb-1">Select Date & Time</label>
                                        <div className="relative">
                                            <input
                                                type="datetime-local"
                                                value={scheduleDate}
                                                onChange={(e) => setScheduleDate(e.target.value)}
                                                className="w-full bg-black/40 border border-white/10 rounded-lg p-2 pl-9 text-sm text-white focus:outline-none focus:border-purple-500/50 [color-scheme:dark]"
                                            />
                                            <Clock size={14} className="absolute left-3 top-2.5 text-zinc-500" />
                                        </div>
                                    </div>
                                )}
                            </div>

                            {/* Platforms */}
                            <div>
                                <label className="block text-xs font-bold text-zinc-400 mb-2">Select Platforms</label>
                                <div className="grid grid-cols-1 gap-2">
                                    <label className="flex items-center gap-3 p-3 bg-white/5 rounded-lg cursor-pointer hover:bg-white/10 transition-colors border border-white/5">
                                        <input type="checkbox" checked={platforms.tiktok} onChange={e => setPlatforms({ ...platforms, tiktok: e.target.checked })} className="w-4 h-4 rounded border-zinc-600 bg-black/50 text-primary focus:ring-primary" />
                                        <div className="flex items-center gap-2 text-sm text-white"><Video size={16} className="text-cyan-400" /> TikTok</div>
                                    </label>
                                    <label className="flex items-center gap-3 p-3 bg-white/5 rounded-lg cursor-pointer hover:bg-white/10 transition-colors border border-white/5">
                                        <input type="checkbox" checked={platforms.instagram} onChange={e => setPlatforms({ ...platforms, instagram: e.target.checked })} className="w-4 h-4 rounded border-zinc-600 bg-black/50 text-primary focus:ring-primary" />
                                        <div className="flex items-center gap-2 text-sm text-white"><Instagram size={16} className="text-pink-400" /> Instagram</div>
                                    </label>
                                    <label className="flex items-center gap-3 p-3 bg-white/5 rounded-lg cursor-pointer hover:bg-white/10 transition-colors border border-white/5">
                                        <input type="checkbox" checked={platforms.youtube} onChange={e => setPlatforms({ ...platforms, youtube: e.target.checked })} className="w-4 h-4 rounded border-zinc-600 bg-black/50 text-primary focus:ring-primary" />
                                        <div className="flex items-center gap-2 text-sm text-white"><Youtube size={16} className="text-red-400" /> YouTube Shorts</div>
                                    </label>
                                </div>
                            </div>
                        </div>

                        {postResult && (
                            <div className={`mb-4 p-3 rounded-lg text-xs flex items-start gap-2 ${postResult.success ? 'bg-green-500/10 text-green-400' : 'bg-red-500/10 text-red-400'}`}>
                                {postResult.success ? <CheckCircle size={14} className="mt-0.5 shrink-0" /> : <AlertCircle size={14} className="mt-0.5 shrink-0" />}
                                <div>{postResult.msg}</div>
                            </div>
                        )}

                        <button
                            onClick={handlePost}
                            disabled={posting || !uploadPostKey}
                            className="w-full py-3 bg-primary hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed rounded-xl text-white font-bold transition-all flex items-center justify-center gap-2"
                        >
                            {posting ? <><Loader2 size={16} className="animate-spin" /> {isScheduling ? 'Scheduling...' : 'Publishing...'}</> : <><Share2 size={16} /> {isScheduling ? 'Schedule Post' : 'Publish Now'}</>}
                        </button>
                    </div>
                </div>
            )}

            <SubtitleModal
                isOpen={showSubtitleModal}
                onClose={() => setShowSubtitleModal(false)}
                onGenerate={handleSubtitle}
                isProcessing={isSubtitling}
                videoUrl={currentVideoUrl}
            />

            <HookModal
                isOpen={showHookModal}
                onClose={() => setShowHookModal(false)}
                onGenerate={handleHook}
                isProcessing={isHooking}
                videoUrl={currentVideoUrl}
                initialText={clip.viral_hook_text}
            />

            {!isLong && (
                <TranslateModal
                    isOpen={showTranslateModal}
                    onClose={() => setShowTranslateModal(false)}
                    onTranslate={handleTranslate}
                    isProcessing={isTranslating}
                    videoUrl={currentVideoUrl}
                    hasApiKey={!!elevenLabsKey}
                />
            )}
        </div>
    );
}
