import React, { useState } from 'react';
import { Youtube, Upload, FileVideo, X, Sparkles, Smartphone, Monitor, Square, UserRound, GalleryHorizontal, SlidersHorizontal, ChevronDown } from 'lucide-react';

const FORMAT_OPTIONS = [
    { id: 'vertical', label: '9:16', hint: 'Shorts / Reels', Icon: Smartphone },
    { id: 'original', label: 'Original', hint: 'Source ratio', Icon: Monitor },
    { id: 'square', label: '1:1', hint: 'Square feed', Icon: Square },
];

const VIDEO_TYPE_OPTIONS = [
    { id: 'shorts', label: 'Viral Clips', hint: '15–60 s · multiple', Icon: Smartphone },
    { id: 'long', label: 'Long Video', hint: '~8–10 min · 16:9', Icon: Monitor },
    { id: 'auto', label: 'Smart / Auto', hint: 'AI chooses the mix', Icon: Sparkles },
];

const LAYOUT_OPTIONS = [
    { id: 'zoom', label: 'Speaker Zoom', hint: 'Always one person', Icon: UserRound },
    { id: 'wide', label: 'Wide', hint: 'Full width + blur', Icon: GalleryHorizontal },
];

export default function MediaInput({ onProcess, isProcessing }) {
    const [mode, setMode] = useState('url'); // 'url' | 'file'
    const [url, setUrl] = useState('');
    const [file, setFile] = useState(null);
    const [videoType, setVideoType] = useState('shorts');
    const [outputFormat, setOutputFormat] = useState('vertical');
    const [layoutStyle, setLayoutStyle] = useState('smart');
    const [showAdvancedLayout, setShowAdvancedLayout] = useState(false);

    const handleSubmit = (e) => {
        e.preventDefault();
        if (mode === 'url' && url) {
            onProcess({ type: 'url', payload: url, outputFormat, layoutStyle, videoType });
        } else if (mode === 'file' && file) {
            onProcess({ type: 'file', payload: file, outputFormat, layoutStyle, videoType });
        }
    };

    const handleDrop = (e) => {
        e.preventDefault();
        if (e.dataTransfer.files && e.dataTransfer.files[0]) {
            setFile(e.dataTransfer.files[0]);
            setMode('file');
        }
    };

    return (
        <div className="bg-surface border border-white/5 rounded-2xl p-6 animate-[fadeIn_0.6s_ease-out]">
            <div className="flex gap-4 mb-6 border-b border-white/5 pb-4">
                <button
                    onClick={() => setMode('url')}
                    className={`flex items-center gap-2 pb-2 px-2 transition-all ${mode === 'url'
                        ? 'text-primary border-b-2 border-primary -mb-[17px]'
                        : 'text-zinc-400 hover:text-white'
                        }`}
                >
                    <Youtube size={18} />
                    YouTube URL
                </button>
                <button
                    onClick={() => setMode('file')}
                    className={`flex items-center gap-2 pb-2 px-2 transition-all ${mode === 'file'
                        ? 'text-primary border-b-2 border-primary -mb-[17px]'
                        : 'text-zinc-400 hover:text-white'
                        }`}
                >
                    <Upload size={18} />
                    Upload File
                </button>
            </div>

            <form onSubmit={handleSubmit}>
                {mode === 'url' ? (
                    <div className="space-y-4">
                        <input
                            type="url"
                            value={url}
                            onChange={(e) => setUrl(e.target.value)}
                            placeholder="https://www.youtube.com/watch?v=..."
                            className="input-field"
                            required
                        />
                    </div>
                ) : (
                    <div
                        className={`border-2 border-dashed rounded-xl p-8 text-center transition-all ${file ? 'border-primary/50 bg-primary/5' : 'border-zinc-700 hover:border-zinc-500 bg-white/5'
                            }`}
                        onDragOver={(e) => e.preventDefault()}
                        onDrop={handleDrop}
                    >
                        {file ? (
                            <div className="flex items-center justify-center gap-3 text-white">
                                <FileVideo className="text-primary" />
                                <span className="font-medium">{file.name}</span>
                                <button
                                    type="button"
                                    onClick={() => setFile(null)}
                                    className="p-1 hover:bg-white/10 rounded-full"
                                >
                                    <X size={16} />
                                </button>
                            </div>
                        ) : (
                            <label className="cursor-pointer block">
                                <input
                                    type="file"
                                    accept="video/*"
                                    onChange={(e) => setFile(e.target.files?.[0] || null)}
                                    className="hidden"
                                />
                                <Upload className="mx-auto mb-3 text-zinc-500" size={24} />
                                <p className="text-zinc-400">Click to upload or drag and drop</p>
                                <p className="text-xs text-zinc-600 mt-1">MP4, MOV up to 500MB</p>
                            </label>
                        )}
                    </div>
                )}

                {/* Video type */}
                <div className="mt-5">
                    <div className="flex items-center justify-between mb-2">
                        <div className="text-xs font-bold text-zinc-400 uppercase tracking-wider">Create</div>
                        <span className="text-[9px] font-mono uppercase tracking-[0.18em] text-zinc-600">Editorial mode</span>
                    </div>
                    <div className="grid grid-cols-3 gap-2">
                        {VIDEO_TYPE_OPTIONS.map(({ id, label, hint, Icon }) => (
                            <button
                                key={id}
                                type="button"
                                onClick={() => setVideoType(id)}
                                aria-pressed={videoType === id}
                                className={`relative flex min-h-[82px] flex-col items-start justify-between overflow-hidden rounded-xl border p-3 text-left transition-all ${videoType === id
                                    ? 'border-primary/70 bg-primary/10 text-white shadow-[inset_0_0_24px_rgba(59,130,246,0.08)]'
                                    : 'border-white/5 bg-white/[0.035] text-zinc-400 hover:border-white/15 hover:bg-white/[0.06] hover:text-white'
                                    }`}
                            >
                                <div className="flex w-full items-center justify-between">
                                    <Icon size={16} className={videoType === id ? 'text-primary' : 'text-zinc-500'} />
                                    <span className={`h-1.5 w-1.5 rounded-full ${videoType === id ? 'bg-primary shadow-[0_0_10px_currentColor]' : 'bg-zinc-700'}`} />
                                </div>
                                <div>
                                    <div className="text-xs font-bold leading-tight">{label}</div>
                                    <div className="mt-1 text-[9px] leading-tight text-zinc-500">{hint}</div>
                                </div>
                            </button>
                        ))}
                    </div>
                </div>

                {/* Output format — Long is always a true 16:9 canvas */}
                {videoType !== 'long' && <div className="mt-5">
                    <div className="text-xs font-bold text-zinc-400 uppercase tracking-wider mb-2">Output Format</div>
                    <div className="grid grid-cols-3 gap-2">
                        {FORMAT_OPTIONS.map(({ id, label, hint, Icon }) => (
                            <button
                                key={id}
                                type="button"
                                onClick={() => setOutputFormat(id)}
                                className={`flex flex-col items-center gap-1 py-2.5 px-1 rounded-xl border text-center transition-all ${outputFormat === id
                                    ? 'border-primary/60 bg-primary/10 text-white'
                                    : 'border-white/5 bg-white/5 text-zinc-400 hover:border-white/15 hover:text-white'
                                    }`}
                            >
                                <Icon size={16} className={outputFormat === id ? 'text-primary' : ''} />
                                <span className="text-xs font-bold">{label}</span>
                                <span className="text-[9px] text-zinc-500 leading-tight">{hint}</span>
                            </button>
                        ))}
                    </div>
                    {videoType === 'auto' && (
                        <p className="mt-2 text-[10px] leading-relaxed text-zinc-500">
                            Applies to Shorts. A generated Long Video always uses a dedicated 16:9 canvas.
                        </p>
                    )}
                </div>}

                {/* Reframing layout — only relevant when the output gets reframed */}
                {videoType !== 'long' && outputFormat !== 'original' && (
                    <div className="mt-4">
                        <div className="flex items-center justify-between mb-2">
                            <div className="text-xs font-bold text-zinc-400 uppercase tracking-wider">Layout</div>
                            <button
                                type="button"
                                onClick={() => setShowAdvancedLayout((open) => !open)}
                                className="flex items-center gap-1.5 text-[10px] font-semibold text-zinc-500 hover:text-zinc-300 transition-colors"
                                aria-expanded={showAdvancedLayout}
                            >
                                <SlidersHorizontal size={12} />
                                Advanced
                                <ChevronDown size={12} className={`transition-transform ${showAdvancedLayout ? 'rotate-180' : ''}`} />
                            </button>
                        </div>

                        <button
                            type="button"
                            onClick={() => setLayoutStyle('smart')}
                            className={`w-full flex items-center gap-3 rounded-xl border px-3 py-2.5 text-left transition-all ${layoutStyle === 'smart'
                                ? 'border-primary/60 bg-primary/10'
                                : 'border-white/5 bg-white/5 hover:border-white/15'
                                }`}
                        >
                            <div className={`grid h-8 w-8 place-items-center rounded-lg ${layoutStyle === 'smart' ? 'bg-primary/15 text-primary' : 'bg-white/5 text-zinc-500'}`}>
                                <Sparkles size={16} />
                            </div>
                            <div className="min-w-0 flex-1">
                                <div className="flex items-center gap-2">
                                    <span className="text-xs font-bold text-white">Smart</span>
                                    <span className="rounded-full bg-emerald-400/10 px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-wider text-emerald-400">Recommended</span>
                                </div>
                                <p className="mt-0.5 text-[10px] leading-snug text-zinc-500">1 person cropped · 2 split · groups wide</p>
                            </div>
                        </button>

                        {showAdvancedLayout && (
                            <div className="grid grid-cols-2 gap-2 mt-2 animate-[fadeIn_0.2s_ease-out]">
                                {LAYOUT_OPTIONS.map(({ id, label, hint, Icon }) => (
                                    <button
                                        key={id}
                                        type="button"
                                        onClick={() => setLayoutStyle(id)}
                                        className={`flex flex-col items-center gap-1 py-2.5 px-1 rounded-xl border text-center transition-all ${layoutStyle === id
                                            ? 'border-primary/60 bg-primary/10 text-white'
                                            : 'border-white/5 bg-white/5 text-zinc-400 hover:border-white/15 hover:text-white'
                                            }`}
                                    >
                                        <Icon size={16} className={layoutStyle === id ? 'text-primary' : ''} />
                                        <span className="text-xs font-bold">{label}</span>
                                        <span className="text-[9px] text-zinc-500 leading-tight">{hint}</span>
                                    </button>
                                ))}
                            </div>
                        )}
                        </div>
                )}

                <button
                    type="submit"
                    disabled={isProcessing || (mode === 'url' && !url) || (mode === 'file' && !file)}
                    className="w-full btn-primary mt-6 flex items-center justify-center gap-2"
                >
                    {isProcessing ? (
                        <>
                            <div className="w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                            Processing Video...
                        </>
                    ) : (
                        <>
                            {videoType === 'long' ? 'Generate Long Video' : videoType === 'auto' ? 'Generate Smart Mix' : 'Generate Clips'}
                        </>
                    )}
                </button>
            </form>
        </div>
    );
}
