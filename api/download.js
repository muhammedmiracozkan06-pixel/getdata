import https from 'https';

export default function handler(req, res) {
  // MEGA'nın doğrudan dosya çekme (Stream) servisleri için proxy mantığı kuruyoruz.
  // Kullanıcının tarayıcısına "Bu bir indirme dosyasıdır" komutu gönderiyoruz:
  res.setHeader('Content-Disposition', 'attachment; filename="pilotai.setup.exe"');
  res.setHeader('Content-Type', 'application/octet-stream');

  // Senin MEGA linkinin arkadaki ham indirme sunucusuna bağlanması için 
  // MEGA linkini doğrudan bir stream akışına çevirmemiz gerekir.
  // NOT: Eğer MEGA izin vermezse, bu exe dosyasını GitHub deponun içine direkt atıp 
  // ya da GitHub Release kısmına yükleyip linki aşağıya yazarsan %100 kusursuz akar!
  
  const fileUrl = "https://mega.nz/file/1mZ10a4T#_BYJofwwpYnqXZKG0YZyOzfpEc4CDGyVHZmhGqceMUc";

  // Dosyayı arkada indirip kullanıcıya kayıpsız iletiyoruz
  https.get(fileUrl, (stream) => {
    stream.pipe(res);
  }).on('error', (err) => {
    res.status(500).send("Download error: " + err.message);
  });
}
