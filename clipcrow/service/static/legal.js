// The Terms and the Privacy Policy, as plain data (built into the page with el() and textContent, never as markup).
// A DRAFT written from what the product really does; the operator must have it reviewed by a lawyer and fill in the entity name and address
// before a public launch (docs/legal-checklist.md). Keep it in step with the code: the third parties and the retention rule below are real.

export const LEGAL_UPDATED = '19 November 2026';
export const CONTACT = 'support@shackyapps.in';

export const LEGAL = {
  terms: {
    title: 'Terms of Service',
    sections: [
      ['Who we are', ['ClipCrow (clipcrow.shackyapps.in) is a service that turns a long video into short vertical clips. It is run by the operator of shackyapps.in ("we", "us"). By using it you agree to these terms and to the Privacy Policy.']],
      ['Your account', ['You sign in with your email address and a one-time code we send you. There are no passwords. Keep your mailbox secure: anyone who can read it can sign in. You must be at least 18 years old, or have a parent or guardian who accepts these terms for you.']],
      ['Your videos and your rights', [
        'You keep all rights in the videos, pictures and other files you upload ("your content") and in the clips made from them. You give us permission to store and process your content only to provide the service to you.',
        'By uploading you confirm that you own the content or have every right and permission needed to process it, including the consent of the people shown or heard where the law requires it.',
        'You are responsible for what you publish. Posting a clip to a social platform is your decision and is governed by that platform\'s rules.']],
      ['What you must not do', [
        'Do not upload content that is illegal, that sexually exploits or endangers children, that infringes someone else\'s rights, or that is used to harass, defraud, impersonate or mislead people.',
        'Do not use the service to identify or track people, to build lookalike or voice-cloning material of a real person without their consent, or to attack, overload or probe the service. We may remove content and close accounts that break these rules.']],
      ['AI', [
        'We use AI models to choose moments, write titles and hooks, read pictures you add, and correct text. Your footage stays real; nothing AI-generated is added unless you switch on AI images, and then you are shown when it is used. A platform may ask you to label such content as AI-made: that duty is yours.',
        'AI can be wrong. Check every clip, caption and title before you publish it.']],
      ['Plans, minutes and payments', [
        'ClipCrow is pay as you go. You recharge a balance with any amount, and each minute of video you upload uses one minute of balance at the price shown on the site and on your Account page. New accounts may get free minutes to try the service. Minutes you buy do not expire. Larger recharges may earn bonus minutes, as shown when you recharge.',
        'Minutes are taken when a job starts and are given back automatically if the job fails or is cancelled. Payments are handled by Razorpay; we never see your card number. Unused minutes can be refunded on request within a reasonable time of the payment; write to ' + CONTACT + '. Prices can change, but a change never affects minutes you have already bought.']],
      ['Keeping and deleting your content', ['Finished jobs are kept for the number of days of your plan and then deleted. You can delete a job, or your whole account, at any time; that removes the files from our storage. We may delete content sooner if we must for legal or safety reasons.']],
      ['Availability and changes', ['We work to keep the service running but do not promise it will always be available or error-free. We may change or end features, and we may change these terms; if a change matters we will tell you by email or on the site. Using the service after a change means you accept it.']],
      ['Our responsibility', ['The service is provided "as is". To the extent the law allows, we are not liable for indirect or consequential loss, lost profit or lost data, and our total liability for any claim is limited to what you paid us in the three months before it. Nothing here limits liability that cannot be limited by law.']],
      ['Law and contact', ['These terms are governed by the laws of India, and the courts at the operator\'s place of business have jurisdiction, unless a mandatory consumer law gives you other rights. Questions or complaints: ' + CONTACT + '.']],
    ],
  },
  privacy: {
    title: 'Privacy Policy',
    sections: [
      ['What we collect', [
        'Your email address and the time you joined; your plan, minutes and the payment and subscription ids Razorpay gives us (no card numbers).',
        'What you upload and what we make from it: videos, pictures, speech transcripts, titles, hooks, captions and clips, your brand settings (logo, colours, ending video, music) and your job history.',
        'Technical logs (time, request, errors, IP address) kept to run, secure and fix the service. We use one strictly necessary cookie to keep you signed in; no advertising or tracking cookies.']],
      ['Faces and voices', ['To frame the speaker and to label who is speaking, the service looks at faces and listens to voices in your video. This is used only to make your clips. We do not identify anyone by name, compare faces or voices with any database, or sell or reuse this data.']],
      ['Who else handles your data', [
        'We use these services to run ClipCrow, each only for its purpose: an AI provider (Anthropic) receives text from your transcript, and pictures you add, to plan the clips; object storage (Cloudflare R2) holds your files; GPU workers do the heavy processing, either a machine we operate or, when the on-demand GPU is used, a rented GPU from Race Engineering that receives your video while the job runs; Resend (or our own mail server) sends our emails; Razorpay handles payments; and, only if you connect it and post, SocioBird and the platforms you choose receive your clip and caption.',
        'We do not sell your data, and we do not use your content to train models.']],
      ['How long we keep it', ['Finished jobs and their files are deleted automatically after the number of days of your plan (shown on your Account page). Deleting a job or your account removes its files from our storage. We keep minimal billing records as long as the law requires.']],
      ['Your choices and rights', [
        'You can delete any job and your whole account yourself on the site. You can ask us to give you a copy of your data, to correct it, or to delete it, by writing to ' + CONTACT + '; we answer within 30 days. You can turn off our optional emails (such as the job-finished mail) on your Account page. Sign-in codes and legal or billing notices are always sent.',
        'If you are in the EU or UK, India or another place with a data-protection law, you also have the rights that law gives you, including the right to complain to the authority.']],
      ['Security', ['Data is sent over HTTPS. Sign-in codes are stored only as a keyed hash and sessions are kept in an HttpOnly cookie. No system is perfectly secure; if a breach affects you we will tell you as the law requires.']],
      ['Children', ['ClipCrow is not for children under 18 and we do not knowingly collect their data.']],
      ['Changes and contact', ['We will update this page when our practice changes and show the date below. Questions: ' + CONTACT + '.']],
    ],
  },
};
