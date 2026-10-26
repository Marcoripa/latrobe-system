import React, { useEffect, useRef, useState } from 'react';
import { Camera, Mic, MicOff, Trash2, CheckCircle2, AlertTriangle } from 'lucide-react';
import { FatButton } from '../../components/ui/FatButton';

/**
 * Step 4: Snap & Speak
 * - Massive Camera button triggering native <input type="file" accept="image/*" capture="environment" />
 * - Massive Microphone button using SpeechRecognition / webkitSpeechRecognition to transcribe into a textarea.
 */
export function Step4Evidence({
  photo,
  onChangePhoto,
  notes = '',
  onChangeNotes,
}) {
  const fileInputRef = useRef(null);
  const recognitionRef = useRef(null);

  const [isListening, setIsListening] = useState(false);
  const [speechError, setSpeechError] = useState('');

  // Clean up speech recognition on unmount
  useEffect(() => {
    return () => {
      if (recognitionRef.current) {
        try {
          recognitionRef.current.stop();
        } catch {
          // ignore stop errors on unmount
        }
      }
    };
  }, []);

  const handleTriggerCamera = () => {
    fileInputRef.current?.click();
  };

  const handlePhotoCapture = (event) => {
    const file = event.target.files?.[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onloadend = () => {
      onChangePhoto({
        name: file.name,
        type: file.type,
        size: file.size,
        capturedAt: new Date().toISOString(),
        dataUrl: reader.result,
      });
    };
    reader.readAsDataURL(file);
  };

  const handleClearPhoto = () => {
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
    onChangePhoto(null);
  };

  const handleToggleSpeech = () => {
    setSpeechError('');

    const SpeechRecognition =
      typeof window !== 'undefined' &&
      (window.SpeechRecognition || window.webkitSpeechRecognition);

    if (!SpeechRecognition) {
      setSpeechError(
        'Voice transcription is not supported on this browser. You can type below.'
      );
      return;
    }

    if (isListening && recognitionRef.current) {
      recognitionRef.current.stop();
      setIsListening(false);
      return;
    }

    try {
      const recognition = new SpeechRecognition();
      recognition.lang = 'en-AU';
      recognition.continuous = false;
      recognition.interimResults = false;

      recognition.onstart = () => {
        setIsListening(true);
      };

      recognition.onresult = (event) => {
        const transcript = Array.from(event.results)
          .map((result) => result[0]?.transcript || '')
          .join(' ')
          .trim();

        if (transcript) {
          const updated = notes ? `${notes.trim()} ${transcript}` : transcript;
          onChangeNotes(updated);
        }
      };

      recognition.onerror = (event) => {
        console.warn('[SpeechRecognition Error]', event.error);
        setSpeechError(
          event.error === 'not-allowed'
            ? 'Microphone permission denied. Allow mic access or type notes.'
            : `Voice capture stopped (${event.error}).`
        );
        setIsListening(false);
      };

      recognition.onend = () => {
        setIsListening(false);
      };

      recognitionRef.current = recognition;
      recognition.start();
    } catch (err) {
      console.error('[SpeechRecognition Init Error]', err);
      setSpeechError('Could not start microphone.');
      setIsListening(false);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <span className="text-xs font-black uppercase tracking-widest text-primary">
          Step 4 of 5
        </span>
        <h2 className="text-2xl sm:text-3xl font-black uppercase tracking-tight text-white mt-1">
          Photo & Notes
        </h2>
      </div>

      {/* 1. Native Environment Camera Capture */}
      <section className="bg-dark-surface border-2 border-dark-border rounded-2xl p-4 space-y-4">
        <div className="text-sm font-black uppercase tracking-widest text-neutral-300">
          Site Photo Evidence
        </div>

        {/* Hidden Native Camera Input */}
        <input
          ref={fileInputRef}
          type="file"
          accept="image/*"
          capture="environment"
          onChange={handlePhotoCapture}
          className="hidden"
          aria-label="Capture site photo with camera"
        />

        {!photo ? (
          <FatButton
            type="button"
            variant="primary"
            icon={Camera}
            onClick={handleTriggerCamera}
            className="min-h-touch-lg text-2xl py-6"
          >
            Snap Site Photo
          </FatButton>
        ) : (
          <div className="space-y-4">
            <div className="relative rounded-xl overflow-hidden border-4 border-primary bg-black">
              <img
                src={photo.dataUrl}
                alt="Captured field work"
                className="w-full h-56 object-cover"
              />
              <div className="bg-black/90 px-4 py-2 flex items-center justify-between border-t-2 border-primary">
                <span className="inline-flex items-center gap-2 text-sm font-black uppercase text-safety-green">
                  <CheckCircle2 className="w-5 h-5 stroke-[2.75]" aria-hidden="true" />
                  Photo Attached
                </span>
                <span className="text-xs font-bold uppercase text-neutral-400 truncate max-w-[180px]">
                  {photo.name}
                </span>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <FatButton
                type="button"
                variant="secondary"
                icon={Camera}
                onClick={handleTriggerCamera}
              >
                Retake
              </FatButton>
              <FatButton
                type="button"
                variant="danger"
                icon={Trash2}
                onClick={handleClearPhoto}
              >
                Remove
              </FatButton>
            </div>
          </div>
        )}
      </section>

      {/* 2. Voice-to-Text Dictation & High-Contrast Notes */}
      <section className="bg-dark-surface border-2 border-dark-border rounded-2xl p-4 space-y-4">
        <div className="flex items-center justify-between">
          <label
            htmlFor="operator-voice-notes"
            className="text-sm font-black uppercase tracking-widest text-neutral-300"
          >
            Field Notes
          </label>
          {isListening && (
            <span className="px-3 py-1 rounded-full bg-safety-red text-white text-xs font-black uppercase tracking-widest animate-pulse">
              Listening Now...
            </span>
          )}
        </div>

        {/* <FatButton
          type="button"
          variant={isListening ? 'danger' : 'orange'}
          icon={isListening ? MicOff : Mic}
          onClick={handleToggleSpeech}
          className="min-h-touch-lg text-2xl py-6"
        >
          {isListening ? 'Tap to Stop Listening' : 'Tap & Speak Notes'}
        </FatButton>

        {speechError && (
          <div
            role="alert"
            className="p-3 rounded-xl bg-black border-2 border-safety-orange text-safety-orange flex items-center gap-3 text-sm font-extrabold uppercase"
          >
            <AlertTriangle className="w-6 h-6 shrink-0 stroke-[2.5]" aria-hidden="true" />
            <span>{speechError}</span>
          </div>
        )} */}

        <textarea
          id="operator-voice-notes"
          rows={4}
          value={notes}
          onChange={(e) => onChangeNotes(e.target.value)}
          placeholder="Tap 'Tap & Speak Notes' above to dictate, or tap here..."
          className="
            w-full p-4 rounded-xl
            bg-black text-white text-xl font-bold
            border-4 border-white
            placeholder:text-neutral-500
            focus:outline-none focus:border-primary focus:ring-4 focus:ring-primary
          "
        />
      </section>
    </div>
  );
}

export default Step4Evidence;
