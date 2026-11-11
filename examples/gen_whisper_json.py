"""Writes examples/synthetic_talk_mr.whisper.json: a SYNTHETIC Marathi talk in OpenAI-Whisper JSON shape.

The text was authored (not transcribed) to look like a real podcast monologue: a sponsor/housekeeping
intro, a strong story with numbers, a tangent, filler, three rules, an outro. Word times are generated
(seeded, speech-like pacing with pauses), so this tests the planner and the pipeline, NOT speech
recognition accuracy. Replace it with a real Whisper transcript for a real evaluation.

    python examples/gen_whisper_json.py [out.json]
"""
import json
import random
import sys

# (text, pause_before_seconds) one entry per sentence / Whisper segment
SENTENCES = [
    ("नमस्कार मंडळी, मी शुभम आणि तुम्ही पाहत आहात आपला पॉडकास्ट.", 0.8),
    ("आजच्या एपिसोडमध्ये आपण बोलणार आहोत छोट्या व्यवसायाबद्दल.", 0.5),
    ("त्याआधी एक छोटीशी सूचना, चॅनेल सबस्क्राइब करा आणि बेल आयकॉन दाबायला विसरू नका.", 0.6),
    ("तर चला सुरू करूया.", 0.5),
    ("मी जेव्हा पहिल्यांदा दुकान सुरू केलं तेव्हा माझ्याकडे फक्त पन्नास हजार रुपये होते.", 2.6),
    ("ते पैसे मी वर्षभराच्या बचतीतून जमवले होते.", 0.7),
    ("पहिल्या तीन महिन्यांत मी सगळे पैसे गमावले.", 0.9),
    ("म्हणजे अक्षरशः एकही ग्राहक आला नाही अशा दिवसांची गणतीच नव्हती.", 0.6),
    ("त्या रात्री मी दुकानाच्या बाहेर बसून रडलो होतो.", 0.8),
    ("पण त्याच रात्री मला एक गोष्ट कळली जी आजपर्यंत माझ्यासोबत आहे.", 0.7),
    ("यश म्हणजे मोठा निर्णय नसतो, यश म्हणजे रोज केलेली छोटी गोष्ट असते.", 0.9),
    ("मी ठरवलं की रोज फक्त एका ग्राहकाला स्वतः फोन करायचा", 0.8),
    ("एकच फोन, फक्त दहा मिनिटं", 0.5),
    ("पहिले दोन आठवडे काहीच झालं नाही.", 0.7),
    ("तिसऱ्या आठवड्यात एका आजींनी मला परत फोन केला आणि म्हणाल्या, बाळा, तुझं दुकान मला खूप आवडलं.", 0.8),
    ("त्या आजींनी आपल्या सोसायटीतल्या पंधरा बायकांना माझा WhatsApp नंबर दिला.", 0.6),
    ("तिथून माझा व्यवसाय बदलला.", 0.6),
    ("आज त्याच दुकानाची उलाढाल मूळ भांडवलाच्या दहा पट आहे.", 0.7),
    ("आणि हे सगळं कोणत्याही मोठ्या जाहिरातीशिवाय झालं", 0.6),
    ("आता तुम्ही विचाराल की सर, पैसे नसतील तर काय करायचं.", 1.1),
    ("माझं उत्तर सोपं आहे, आधी वेळ गुंतवा.", 0.6),
    ("वेळ हे असं भांडवल आहे जे कोणाकडेही कमी नसतं.", 0.6),
    ("पण बहुतेक लोक तो वेळ मोबाईलवर घालवतात.", 0.6),
    ("मी स्वतः पूर्वी रोज तीन तास रील्स बघायचो.", 0.7),
    ("जेव्हा ते तीन तास मी ग्राहकांशी बोलण्यात घालवले तेव्हा सगळं बदललं.", 0.7),
    ("तर, अं, म्हणजे, हा विषय खूप मोठा आहे आणि त्यावर आपण पुढच्या भागात बोलू", 2.0),
    ("आमच्या स्पॉन्सरबद्दल सांगायचं झालं तर, हा भाग सादर करत आहे आपल्या आवडत्या शेअर मार्केट अॅपतर्फे.", 0.7),
    ("तुम्ही त्यांच्या वेबसाईटवर जाऊन आमचा कोड वापरू शकता.", 0.5),
    ("आता माझे तीन नियम ऐका, जे मी प्रत्येक नवीन उद्योजकाला सांगतो.", 1.4),
    ("पहिला नियम, कधीही उधारीवर विक्री करू नका.", 0.8),
    ("दुसरा नियम, आपला हिशोब रोज रात्री लिहा, अगदी पाच मिनिटं का असेना.", 0.8),
    ("तिसरा नियम, तुमचा ग्राहक तुम्हाला काय सांगतो ते लिहून ठेवा, कारण तिथेच तुमची पुढची कल्पना लपलेली असते.", 0.8),
    ("मी हे तिन्ही नियम पाळले आणि दोन वर्षांत माझं कर्ज पूर्ण फेडलं.", 0.9),
    ("कर्ज होतं बारा लाख रुपये, आणि लोकांनी मला सांगितलं होतं की हे अशक्य आहे.", 0.7),
    ("पण अशक्य काहीच नसतं, फक्त सातत्य लागतं.", 0.8),
    ("सातत्य म्हणजे कंटाळा आल्यावरही तेच काम पुन्हा करणं.", 0.8),
    ("तर मंडळी, आजचा भाग इथे संपतो.", 1.2),
    ("तुमच्या प्रतिक्रिया कमेंटमध्ये नक्की लिहा.", 0.5),
    ("पुढच्या भागात भेटू, तोपर्यंत स्वतःची काळजी घ्या.", 0.6),
]


def build(seed: int = 7) -> dict:
    rnd = random.Random(seed)
    t = 0.6
    segments, full = [], []
    for sid, (text, pause) in enumerate(SENTENCES):
        t += pause
        words, start = [], t
        for tok in text.split():
            dur = min(0.75, max(0.18, 0.12 + 0.055 * len(tok)))
            s, e = t, t + dur
            prob = rnd.uniform(0.72, 0.99) if rnd.random() > 0.04 else rnd.uniform(0.35, 0.55)
            words.append({"word": " " + tok, "start": round(s, 2), "end": round(e, 2), "probability": round(prob, 3)})
            t = e + rnd.uniform(0.03, 0.12)
        segments.append({"id": sid, "start": round(start, 2), "end": words[-1]["end"], "text": " " + text,
                         "words": words})
        full.append(text)
    return {"text": " " + " ".join(full), "language": "mr", "duration": round(t + 1.5, 2), "segments": segments}


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "examples/synthetic_talk_mr.whisper.json"
    data = build()
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    n = sum(len(s["words"]) for s in data["segments"])
    print(f"{out}: {len(data['segments'])} segments, {n} words, {data['duration']:.0f}s")
