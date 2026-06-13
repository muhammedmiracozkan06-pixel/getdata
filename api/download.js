export default function handler(req, res) {
  const host = req.headers.host; // Gelen isteğin domain adresi
  const megaLink = "https://mega.nz/file/ku5iibiJ#r7fnVCvhuyJHkyKvYOBqqFmJA-Hw9sxqg5dVNktQUbE";

  // Eğer istek tam olarak senin belirttiğin download alt domaininden geldiyse
  if (host === "download.pulldata.duckdns.org") {
    return res.redirect(302, megaLink);
  }

  // Eğer normal pulldata.duckdns.org üzerinden geldiyse ana sayfaya ya da apiye yönlendirsin
  res.status(200).json({ message: "Pulldata API Merkezine Hoş Geldiniz!" });
}
