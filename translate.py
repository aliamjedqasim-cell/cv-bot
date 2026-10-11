"""Arabic -> English for CV answers.

Order of preference for each kind of answer:
  1. Built-in dictionaries (Iraqi cities, universities, specialties, job titles, names, products):
     exact and free.
  2. Free machine translation (MyMemory API, no key needed). Optional MYMEMORY_EMAIL env raises
     the free daily limit from 5,000 to 50,000 characters.
  3. Letter-by-letter transliteration, so nothing Arabic ever reaches the PDF.

Every function returns (english_text, how) where how is "latin", "dict", "mt" or "translit".
"""
import html
import os
import re
import threading

import requests

ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_DIAC = re.compile(r"[ً-ْٰـ]")

http = requests.Session()
MT_URL = "https://api.mymemory.translated.net/get"
_cache = {}
_cache_lock = threading.Lock()


def has_ar(text):
    return bool(ARABIC.search(text or ""))


def clean(text):
    return " ".join((text or "").replace("‏", "").replace("‎", "").split())


def norm(text):
    """Arabic spelling-insensitive key: no diacritics, unified alef/ya/ta marbuta, Iraqi letters."""
    t = _DIAC.sub("", clean(text))
    t = re.sub("[إأآٱ]", "ا", t)
    for a, b in (("ة", "ه"), ("ى", "ي"), ("ؤ", "و"), ("ئ", "ي"), ("گ", "ك"), ("ڤ", "ف"), ("چ", "ج"), ("پ", "ب")):
        t = t.replace(a, b)
    return t.translate(AR_DIGITS).strip(" .،,-")


def _table(pairs):
    out = {}
    for ar, en in pairs.items():
        for variant in ar.split("|"):
            out[norm(variant)] = en
    return out


# ---------------------------------------------------------------- dictionaries
PLACES = _table({
    "بغداد": "Baghdad", "البصره|بصره|البصرة": "Basra", "الموصل|موصل": "Mosul", "نينوى|نينوي": "Nineveh",
    "اربيل|أربيل": "Erbil", "السليمانيه|سليمانيه": "Sulaymaniyah", "دهوك": "Duhok", "كركوك": "Kirkuk",
    "ديالى|ديالي": "Diyala", "بعقوبه": "Baqubah", "الانبار|انبار": "Anbar", "الرمادي|رمادي": "Ramadi",
    "الفلوجه|فلوجه": "Fallujah", "بابل": "Babylon", "الحله|حله": "Hillah", "كربلاء|كربلا": "Karbala",
    "النجف|نجف|النجف الاشرف": "Najaf", "الديوانيه|ديوانيه": "Diwaniyah", "القادسيه": "Al-Qadisiyah",
    "واسط": "Wasit", "الكوت|كوت": "Kut", "ميسان": "Maysan", "العماره|عماره": "Amarah",
    "ذي قار|ذيقار": "Dhi Qar", "الناصريه|ناصريه": "Nasiriyah", "المثنى|المثني": "Al-Muthanna",
    "السماوه|سماوه": "Samawah", "صلاح الدين": "Salahuddin", "تكريت": "Tikrit", "سامراء|سامرا": "Samarra",
    "الكوفه|كوفه": "Kufa", "الزبير": "Al-Zubair", "ابو الخصيب": "Abu Al-Khaseeb", "الشطره": "Al-Shatra",
    # Baghdad areas
    "الكرخ|كرخ|جانب الكرخ": "Karkh", "الرصافه|رصافه|جانب الرصافه": "Rusafa", "المنصور|منصور": "Al-Mansour",
    "الكراده|كراده": "Karrada", "الاعظميه|اعظميه": "Adhamiyah", "الكاظميه|كاظميه": "Kadhimiya",
    "زيونه": "Zayouna", "الجادريه|جادريه": "Al-Jadriya", "اليرموك": "Al-Yarmouk", "الدوره|دوره": "Al-Dora",
    "البياع|بياع": "Al-Bayaa", "الشعب": "Al-Shaab", "مدينه الصدر|الصدر": "Sadr City", "الحريه": "Al-Hurriya",
    "الغزاليه|غزاليه": "Al-Ghazaliya", "العامريه|عامريه": "Al-Amiriya", "حي الجامعه": "Hay Al-Jamia",
    "شارع فلسطين|فلسطين": "Palestine Street", "بغداد الجديده": "New Baghdad", "الزعفرانيه": "Al-Zafaraniya",
    "الشعله": "Al-Shula", "السيديه": "Al-Saydiya", "الحارثيه": "Al-Harthiya", "الوزيريه": "Al-Waziriya",
    "باب المعظم": "Bab Al-Muadham", "الامين": "Al-Ameen", "حي الجهاد|الجهاد": "Al-Jihad", "العلاوي": "Al-Allawi",
    "الباب الشرقي": "Bab Al-Sharqi", "الكسره": "Al-Kasra", "المشتل": "Al-Mashtal", "البلديات": "Al-Baladiyat",
    "الحسينيه": "Al-Husseiniya", "الشعب": "Al-Shaab", "الطالبيه": "Al-Talibiya", "الاسكان": "Al-Iskan",
    "العطيفيه": "Al-Utaifiya", "الصالحيه": "Al-Salihiya", "الداوودي|الداودي": "Al-Dawoodi",
    "ابو غريب": "Abu Ghraib", "التاجي": "Al-Taji", "المحموديه": "Al-Mahmudiya", "الشعب": "Al-Shaab",
    "كل العراق|العراق|جميع المحافظات": "All Iraqi Governorates", "المحافظه": "the Governorate",
    "الوسط": "Central Iraq", "الجنوب|المحافظات الجنوبيه": "Southern Iraq", "الشمال": "Northern Iraq",
    "الفرات الاوسط": "Middle Euphrates",
})

UNIVERSITIES = _table({
    "جامعه بغداد": "University of Baghdad", "الجامعه المستنصريه|جامعه المستنصريه|المستنصريه": "Mustansiriyah University",
    "الجامعه التكنولوجيه|التكنولوجيه": "University of Technology", "جامعه النهرين|النهرين": "Al-Nahrain University",
    "جامعه البصره": "University of Basrah", "جامعه الكوفه": "University of Kufa", "جامعه بابل": "University of Babylon",
    "جامعه كربلاء|جامعه كربلا": "University of Kerbala", "جامعه الموصل": "University of Mosul",
    "جامعه الانبار": "University of Anbar", "جامعه ديالى": "University of Diyala", "جامعه واسط": "Wasit University",
    "جامعه ذي قار": "University of Thi-Qar", "جامعه ميسان": "University of Misan",
    "جامعه القادسيه": "University of Al-Qadisiyah", "جامعه المثنى": "Al-Muthanna University",
    "جامعه تكريت": "Tikrit University", "جامعه كركوك": "University of Kirkuk", "جامعه صلاح الدين": "Salahaddin University",
    "جامعه السليمانيه": "University of Sulaimani", "جامعه دهوك": "University of Duhok",
    "الجامعه العراقيه|العراقيه": "Al-Iraqia University", "جامعه سامراء": "University of Samarra",
    "جامعه الفرات الاوسط التقنيه": "Al-Furat Al-Awsat Technical University",
    "الجامعه التقنيه الوسطى|التقنيه الوسطى": "Middle Technical University",
    "الجامعه التقنيه الجنوبيه|التقنيه الجنوبيه": "Southern Technical University",
    "الجامعه التقنيه الشماليه": "Northern Technical University",
    "كليه الرافدين الجامعه|جامعه الرافدين|كليه الرافدين|الرافدين": "Al-Rafidain University College",
    "كليه المامون الجامعه|جامعه المامون|كليه المامون|المامون": "Al-Mamoun University College",
    "جامعه التراث|كليه التراث الجامعه|كليه التراث|التراث": "Al-Turath University",
    "كليه دجله الجامعه|جامعه دجله|كليه دجله|دجله": "Dijlah University College",
    "جامعه الاسراء|كليه الاسراء|الاسراء": "Al-Esraa University", "جامعه الكفيل|كليه الكفيل": "Al-Kafeel University",
    "جامعه العميد": "University of Alameed", "جامعه الزهراء للبنات|جامعه الزهراء": "Al-Zahraa University for Women",
    "جامعه الامام جعفر الصادق|جامعه الصادق": "Imam Ja'afar Al-Sadiq University",
    "كليه المنصور الجامعه|كليه المنصور": "Al-Mansour University College",
    "كليه بغداد للعلوم الطبيه": "Baghdad College of Medical Sciences", "جامعه اهل البيت": "Ahl Al-Bayt University",
    "جامعه الكتاب": "Alkitab University", "جامعه الفارابي|كليه الفارابي|الفارابي": "Al-Farabi University",
    "كليه اليرموك الجامعه|كليه اليرموك": "Al-Yarmouk University College", "جامعه جيهان": "Cihan University",
    "جامعه المستقبل|كليه المستقبل": "Al-Mustaqbal University", "كليه الحله الجامعه|جامعه الحله": "Hilla University College",
    "جامعه وارث الانبياء": "University of Warith Al-Anbiyaa", "جامعه الامام الكاظم|كليه الامام الكاظم": "Imam Al-Kadhim University",
    "كليه الحكمه الجامعه|كليه الحكمه": "Al-Hikma University College", "جامعه بلاد الرافدين": "Bilad Alrafidain University",
    "كليه التقنيات الصحيه والطبيه": "College of Health and Medical Technology",
    "معهد التقنيات الطبيه": "Institute of Medical Technology", "المعهد التقني": "Technical Institute",
    "جامعه الشعب": "Al-Shaab University", "جامعه البيان": "Al-Bayan University", "جامعه اوروك": "Uruk University",
    "كليه الكوت الجامعه": "Al-Kut University College", "جامعه العين": "Al-Ayen University",
    "كليه الصفوه الجامعه": "Al-Safwa University College", "جامعه النسور|كليه النسور": "Al-Nisour University",
})

SPECIALTIES = _table({
    "صيدله": "Pharmacy", "طب|طب عام|طب وجراحه": "Medicine and Surgery", "طب اسنان|الاسنان": "Dentistry",
    "تمريض": "Nursing", "تحليلات مرضيه|تحليلات|التحليلات المرضيه": "Medical Laboratory Sciences",
    "تقنيات تحليلات مرضيه|تقنيات التحليلات المرضيه": "Pathological Analysis Techniques",
    "مختبرات|علوم مختبرات|مختبرات طبيه": "Medical Laboratory Sciences", "علوم مختبرات سريريه": "Clinical Laboratory Sciences",
    "علوم حياه|علوم الحياه|احياء|علم الاحياء|بايولوجي": "Biology", "احياء مجهريه": "Microbiology",
    "كيمياء|كيمياء عامه": "Chemistry", "كيمياء حياتيه|كيمياء سريريه": "Biochemistry", "فيزياء": "Physics",
    "فيزياء طبيه": "Medical Physics", "تقنيات احيائيه|بايوتكنولوجي|تقانه احيائيه": "Biotechnology",
    "تقنيات اشعه|اشعه": "Radiology Techniques", "تقنيات تخدير|تخدير": "Anesthesia Techniques",
    "بصريات|تقنيات بصريات": "Optometry", "صحه مجتمع|تقنيات صحه مجتمع": "Community Health",
    "علاج طبيعي": "Physiotherapy", "تغذيه|علوم تغذيه": "Nutrition", "طب بيطري": "Veterinary Medicine",
    "ادويه|تقنيات صيدلانيه": "Pharmaceutical Techniques", "صحه عامه": "Public Health",
    "ادراه اعمال|اداره الاعمال|اداره": "Business Administration", "محاسبه": "Accounting", "اقتصاد": "Economics",
    "تسويق": "Marketing", "علوم ماليه ومصرفيه|ماليه ومصرفيه": "Banking and Financial Sciences",
    "اداره صحيه|اداره مستشفيات": "Health Administration", "قانون": "Law", "علوم سياسيه": "Political Science",
    "هندسه مدنيه|مدني": "Civil Engineering", "هندسه كهربائيه|هندسه كهرباء|كهرباء": "Electrical Engineering",
    "هندسه ميكانيكيه|ميكانيك": "Mechanical Engineering", "هندسه نفط|نفط": "Petroleum Engineering",
    "هندسه كيمياويه|هندسه كيميائيه": "Chemical Engineering",
    "هندسه طبيه|هندسه الطب الحياتي|هندسه طب حياتي|هندسه الاجهزه الطبيه|اجهزه طبيه": "Biomedical Engineering",
    "هندسه حاسبات|هندسه الحاسوب": "Computer Engineering", "هندسه معماريه|عماره": "Architectural Engineering",
    "هندسه اتصالات|اتصالات": "Communications Engineering", "هندسه برمجيات": "Software Engineering",
    "علوم حاسبات|حاسبات|علوم الحاسوب|حاسوب": "Computer Science", "تقنيه معلومات|تكنولوجيا المعلومات": "Information Technology",
    "لغه انكليزيه|انكليزي|اللغه الانكليزيه|لغه انجليزيه": "English Language", "ترجمه": "Translation",
    "اعلام|صحافه": "Media and Journalism", "رياضيات": "Mathematics", "تربيه رياضيه": "Physical Education",
    "جغرافيه": "Geography", "تاريخ": "History", "علم نفس": "Psychology", "زراعه": "Agriculture",
    "علوم قران|علوم اسلاميه": "Islamic Studies", "لغه عربيه": "Arabic Language", "تربيه": "Education",
    "اداب": "Arts", "علمي": "Scientific Branch", "ادبي": "Literary Branch",
})

TITLES = _table({
    "مندوب طبي|ممثل طبي|مندوب علمي|مندوبه طبيه": "Medical Representative",
    "مندوب مبيعات طبي|مندوب مبيعات طبيه|مندوب مبيعات طبيه وتجميليه": "Medical Sales Representative",
    "مندوب مبيعات|مندوب|مندوبه مبيعات|مندوب بيع": "Sales Representative", "مشرف مبيعات|مشرف": "Sales Supervisor",
    "مدير مبيعات": "Sales Manager", "مدير منطقه": "Area Manager", "مسوق|مسوق ميداني|مندوب تسويق": "Marketing Representative",
    "اخصائي تسويق|مختص تسويق": "Marketing Specialist", "مدير تسويق": "Marketing Manager",
    "صيدلاني|صيدلي|صيدلانيه": "Pharmacist", "مساعد صيدلاني|مساعد صيدلي": "Pharmacy Assistant",
    "اخصائي مختبر|محلل مختبر|محلل|مختبري": "Laboratory Specialist", "ممرض|ممرضه": "Nurse",
    "محاسب|محاسبه": "Accountant", "كاشير": "Cashier", "خدمه عملاء|موظف خدمه عملاء": "Customer Service Representative",
    "سكرتير|سكرتيره": "Secretary", "مدخل بيانات": "Data Entry Clerk", "موظف استقبال|استقبال": "Receptionist",
    "مروج|مروجه|مروج مبيعات": "Sales Promoter", "بائع|بايع": "Sales Associate", "مدير": "Manager",
    "موظف": "Employee", "متدرب": "Trainee", "مساعد": "Assistant", "مندوب توزيع|موزع": "Distribution Representative",
    "سائق": "Driver", "مدير مكتب": "Office Manager", "معلم|مدرس": "Teacher",
})

PRODUCTS = _table({
    "فلر|فلرات|فيلر": "Dermal Fillers", "بوتوكس": "Botox", "ابر|حقن|سرنجات|محاقن": "Medical Needles & Syringes",
    "مضادات حيويه|مضاد حيوي": "Antibiotics", "ادويه القلب|قلب|القلب": "Cardiovascular Medicines",
    "ادويه السكري|سكري|السكري": "Diabetes Medicines", "ادويه الضغط|ضغط": "Antihypertensives",
    "مستحضرات تجميل|تجميل|مستحضرات تجميليه": "Cosmetics", "مستلزمات طبيه": "Medical Supplies",
    "اجهزه طبيه": "Medical Devices", "فيتامينات": "Vitamins", "مكملات غذائيه|مكملات": "Nutritional Supplements",
    "جلديه|ادويه جلديه": "Dermatology Products", "العنايه بالبشره|عنايه بشره": "Skin Care",
    "حليب اطفال": "Infant Formula", "ادويه|الادويه": "Pharmaceuticals", "مسكنات": "Analgesics",
    "مستلزمات اسنان|مواد اسنان": "Dental Supplies", "عدسات": "Contact Lenses", "مغذيات|محاليل": "IV Solutions",
    "مختبرات|مواد مختبريه|عدد مختبريه": "Laboratory Supplies", "ادويه نسائيه|نسائيه": "Gynecology Products",
    "ادويه اطفال|اطفال": "Pediatric Medicines", "عظام|ادويه عظام": "Orthopedic Products",
    "عيون|ادويه عيون": "Ophthalmic Products", "اعصاب|ادويه اعصاب": "Neurology Medicines",
    "معجون اسنان": "Oral Care", "شامبو|العنايه بالشعر": "Hair Care", "ميزوثيرابي": "Mesotherapy",
    "خيوط تجميليه|خيوط": "PDO Threads", "بلازما": "PRP Products", "ليزر|اجهزه ليزر": "Laser Devices",
})

BRANDS = _table({
    "بيونير|بايونير": "Pioneer", "حكمه|الحكمه": "Hikma", "جلفار": "Julphar", "سانوفي": "Sanofi",
    "نوفارتس|نوفارتيس": "Novartis", "فايزر": "Pfizer", "باير": "Bayer", "روش": "Roche",
    "استرازينيكا|استرازينكا": "AstraZeneca", "ميرك": "Merck", "ابوت": "Abbott", "سيرفيه": "Servier",
    "الكندي": "Al-Kindi", "اسيا سيل|اسياسيل": "Asiacell", "زين": "Zain", "كورك": "Korek", "تبوك": "Tabuk",
    "دار الدواء": "Dar Al Dawa", "جمجوم": "Jamjoom", "اكديما": "Acdima", "سامراء": "Samarra",
    "اوميغا|اوميجا": "Omega", "بيوكير": "BioCare", "جونسون": "Johnson & Johnson", "لوريال": "L'Oreal",
    "نستله": "Nestle", "يونيليفر": "Unilever", "بروكتر": "Procter & Gamble", "سامسونج|سامسونك": "Samsung",
    "تويوتا": "Toyota", "كيا": "Kia", "هيونداي": "Hyundai", "ايرث لنك|ايرثلنك": "Earthlink",
})

GENERIC_ORG = _table({  # company words that should be translated, not transliterated
    "شركه": "Company", "صيدليه": "Pharmacy", "مذخر|مذخر ادويه": "Pharmaceutical Warehouse",
    "مكتب": "Office", "مكتب علمي": "Scientific Office", "مؤسسه|موسسه": "Foundation", "مجموعه": "Group",
    "مستشفى|مستشفي": "Hospital", "عياده|عيادات": "Clinic", "مختبر": "Laboratory", "مركز": "Center",
    "مجمع": "Complex", "معرض": "Showroom", "محل": "Shop", "الادويه|ادويه": "Pharmaceuticals",
    "التجاره|تجاره": "Trading", "العلمي": "Scientific", "الطبيه|طبيه|الطبي|طبي": "Medical",
    "التجميل|تجميل": "Cosmetics", "للتجاره العامه|التجاره العامه": "General Trading",
})

NAMES = _table({
    "علي": "Ali", "محمد": "Mohammed", "احمد": "Ahmed", "محمود": "Mahmoud", "حسن": "Hassan", "حسين": "Hussein",
    "حسنين": "Hasanain", "عباس": "Abbas", "جعفر": "Jaafar", "كاظم": "Kadhim", "مصطفى": "Mustafa", "مرتضى": "Murtadha",
    "مجتبى": "Mujtaba", "منتظر": "Muntadher", "مهدي": "Mahdi", "هادي": "Hadi", "سجاد": "Sajjad", "حيدر": "Haider",
    "كرار": "Karrar", "زين": "Zain", "زين العابدين": "Zain Al-Abideen", "امير": "Ameer", "امجد": "Amjad",
    "قاسم": "Qasim", "جاسم": "Jasim", "كريم": "Kareem", "رحيم": "Raheem", "سليم": "Saleem", "سالم": "Salem",
    "ناصر": "Nasser", "نصير": "Naseer", "جمال": "Jamal", "كمال": "Kamal", "فاضل": "Fadhil", "عادل": "Adel",
    "خالد": "Khalid", "وليد": "Waleed", "سعد": "Saad", "سعيد": "Saeed", "عمر": "Omar", "عثمان": "Othman",
    "يوسف": "Yousif", "ابراهيم": "Ibrahim", "اسماعيل": "Ismail", "يعقوب": "Yaqoob", "موسى": "Mousa", "عيسى": "Eisa",
    "يحيى": "Yahya", "زكريا": "Zakaria", "ادم": "Adam", "صالح": "Saleh", "فلاح": "Falah", "نجاح": "Najah",
    "رياض": "Riyadh", "فراس": "Firas", "احسان": "Ihsan", "حسام": "Hussam", "هشام": "Hisham", "عصام": "Essam",
    "وسام": "Wisam", "بسام": "Bassam", "سامي": "Sami", "رامي": "Rami", "هاني": "Hani", "ماهر": "Maher",
    "طارق": "Tariq", "زياد": "Ziyad", "نزار": "Nizar", "عمار": "Ammar", "ياسر": "Yasir", "باسم": "Basim",
    "جواد": "Jawad", "مراد": "Murad", "صادق": "Sadiq", "باقر": "Baqir", "رضا": "Ridha", "مسلم": "Muslim",
    "عقيل": "Aqeel", "نبيل": "Nabeel", "جليل": "Jaleel", "خليل": "Khalil", "اياد": "Ayad", "ضياء": "Dhiaa",
    "علاء": "Alaa", "سيف": "Saif", "ليث": "Laith", "غيث": "Ghaith", "مؤمل": "Muammal", "مؤيد": "Muayad",
    "محسن": "Muhsin", "حميد": "Hameed", "مجيد": "Majeed", "رشيد": "Rasheed", "شاكر": "Shakir", "ثامر": "Thamer",
    "عامر": "Amer", "عمران": "Imran", "اكرم": "Akram", "اسامه": "Osama", "حمزه": "Hamza", "طه": "Taha",
    "ياسين": "Yaseen", "نور الدين": "Nooruldeen", "صفاء": "Safaa", "وائل": "Wael", "مازن": "Mazin",
    "منير": "Muneer", "سمير": "Sameer", "كامل": "Kamil", "نعمان": "Numan", "لؤي": "Luay", "فيصل": "Faisal",
    "انس": "Anas", "بلال": "Bilal", "سلام": "Salam", "مهند": "Muhannad", "اسعد": "Asaad", "عدنان": "Adnan",
    "فؤاد": "Fuad", "جبار": "Jabbar", "ستار": "Sattar", "عزيز": "Aziz", "يونس": "Younis", "قيس": "Qais",
    "حارث": "Harith", "ميثم": "Maitham", "هيثم": "Haitham", "رائد": "Raed", "ماجد": "Majid", "زهير": "Zuhair",
    "فارس": "Faris", "حازم": "Hazim", "نجم": "Najm", "هاشم": "Hashim", "جابر": "Jabir", "ثائر": "Thaer",
    "سرمد": "Sarmad", "موفق": "Muwafaq", "ضرغام": "Dhurgham", "علوان": "Alwan", "شهاب": "Shihab",
    "سلمان": "Salman", "رسول": "Rasool", "حمودي": "Hamoodi", "منصور": "Mansour", "نوري": "Noori",
    "صباح": "Sabah", "جلال": "Jalal", "بشير": "Basheer", "نذير": "Natheer", "مثنى": "Muthanna", "احمد": "Ahmed",
    "حسوني": "Hassoni", "عبود": "Aboud", "كاطع": "Katea", "جبر": "Jabr", "شنان": "Shanan", "غازي": "Ghazi",
    "داود": "Dawood", "سليمان": "Sulaiman", "ايوب": "Ayoub", "هارون": "Haroon", "نوح": "Nooh", "عون": "Aoun",
    "فاطمه": "Fatima", "زينب": "Zainab", "زهراء": "Zahraa", "الزهراء": "Al-Zahraa", "مريم": "Maryam", "نور": "Noor",
    "نورا": "Nora", "هدى": "Huda", "ساره": "Sara", "رقيه": "Ruqaya", "ايه": "Aya", "اسراء": "Israa", "دعاء": "Duaa",
    "ضحى": "Dhuha", "رسل": "Rusul", "بنين": "Baneen", "حوراء": "Hawraa", "تبارك": "Tabarak", "شهد": "Shahad",
    "غدير": "Ghadeer", "سجى": "Saja", "ريام": "Riyam", "مروه": "Marwa", "هبه": "Hiba", "رنا": "Rana", "دينا": "Dina",
    "لينا": "Lina", "ريم": "Reem", "ناديه": "Nadia", "سعاد": "Suad", "صفا": "Safa", "اسيل": "Aseel", "ايات": "Ayat",
    "مينا": "Mina", "نبا": "Naba", "فرح": "Farah", "حنين": "Haneen", "رغد": "Raghad", "اسماء": "Asmaa",
    "انفال": "Anfal", "زينه": "Zeina", "شيماء": "Shaimaa", "ابتهال": "Ibtihal", "حلا": "Hala", "هاله": "Hala",
    "ولاء": "Walaa", "الاء": "Alaa", "زهره": "Zahra", "اطياف": "Atyaf", "وجدان": "Wijdan", "ايمان": "Iman",
    "امنه": "Amna", "خديجه": "Khadija", "عائشه": "Aisha", "ليلى": "Laila", "سلمى": "Salma", "تقى": "Tuqa",
    "ملاك": "Malak", "رحاب": "Rihab", "بتول": "Batool", "كوثر": "Kawthar", "زهراء": "Zahraa", "سرى": "Sura",
    "عبدالله|عبد الله": "Abdullah", "عبد الرحمن|عبدالرحمن": "Abdulrahman", "عبد الكريم|عبدالكريم": "Abdulkareem",
    "عبد الحسين|عبدالحسين": "Abdulhussein", "عبد الامير|عبدالامير": "Abdulameer", "عبد الزهره|عبدالزهره": "Abdulzahra",
    "عبد الرضا|عبدالرضا": "Abdulridha", "عبد الستار|عبدالستار": "Abdulsattar", "عبد الجبار|عبدالجبار": "Abduljabbar",
    "عبد العزيز|عبدالعزيز": "Abdulaziz", "عبد الرزاق|عبدالرزاق": "Abdulrazzaq", "عبد الحميد|عبدالحميد": "Abdulhameed",
    "عبد المجيد|عبدالمجيد": "Abdulmajeed", "عبد الواحد|عبدالواحد": "Abdulwahid", "عبد الهادي|عبدالهادي": "Abdulhadi",
    "عبد علي|عبدعلي": "Abdali", "عبد الوهاب|عبدالوهاب": "Abdulwahab", "عبد السلام|عبدالسلام": "Abdulsalam",
    "الربيعي": "Al-Rubaie", "الساعدي": "Al-Saadi", "العبيدي": "Al-Obaidi", "الجبوري": "Al-Jubouri",
    "التميمي": "Al-Tamimi", "الدليمي": "Al-Dulaimi", "الخفاجي": "Al-Khafaji", "الموسوي": "Al-Mousawi",
    "الحسيني": "Al-Husseini", "الكعبي": "Al-Kaabi", "الزبيدي": "Al-Zubaidi", "الشمري": "Al-Shammari",
    "العزاوي": "Al-Azzawi", "البياتي": "Al-Bayati", "الطائي": "Al-Taie", "الكناني": "Al-Kinani", "اللامي": "Al-Lami",
    "المالكي": "Al-Maliki", "العامري": "Al-Amiri", "الجنابي": "Al-Janabi", "العكيلي|العقيلي": "Al-Ugaili",
    "الفتلاوي": "Al-Fatlawi", "الحلي": "Al-Hilli", "البصري": "Al-Basri", "الياسري": "Al-Yasiri", "الغزي": "Al-Ghazi",
    "السوداني": "Al-Sudani", "الكبيسي": "Al-Kubaisi", "الانصاري": "Al-Ansari", "الحمداني": "Al-Hamdani",
    "الخزرجي": "Al-Khazraji", "العنزي": "Al-Anazi", "المحمداوي": "Al-Mohammedawi", "الحسناوي": "Al-Hasnawi",
    "الشويلي": "Al-Shuwaili", "الركابي": "Al-Rikabi", "البدري": "Al-Badri", "الاسدي": "Al-Asadi",
    "الخالدي": "Al-Khalidi", "الدراجي": "Al-Daraji", "العيساوي": "Al-Issawi", "البهادلي": "Al-Bahadli",
    "الشيباني": "Al-Shaibani", "الهاشمي": "Al-Hashimi", "الحيدري": "Al-Haidari", "الجعفري": "Al-Jaafari",
    "حكمه": "Hikma", "فرات": "Furat", "رافدين": "Rafidain", "دجله": "Dijla", "شفاء": "Shifa", "امل": "Amal",
    "حياه": "Hayat", "سلامه": "Salama", "رحمه": "Rahma", "بركه": "Baraka", "نخيل": "Nakheel", "عراق": "Iraq",
    "اوروك": "Uruk", "سومر": "Sumer", "اشور": "Ashur", "نهرين": "Nahrain", "مستقبل": "Mustaqbal", "ريان": "Rayan",
    "نجوم": "Nujoom", "قمر": "Qamar", "شمس": "Shams", "وطن": "Watan", "اريج": "Areej", "ياسمين": "Yasmine",
    "زهور": "Zuhoor", "صحه": "Sehha", "دواء": "Dawaa", "طب": "Tib", "عافيه": "Afia", "بلسم": "Balsam",
    "السعدي": "Al-Saadi", "الكرعاوي": "Al-Karawi", "الشمخاني": "Al-Shamkhani", "الزيدي": "Al-Zaidi",
    "الكاظمي": "Al-Kadhimi", "النجفي": "Al-Najafi", "الكربلائي": "Al-Karbalaei", "الموصلي": "Al-Mousili",
    "البغدادي": "Al-Baghdadi", "العراقي": "Al-Iraqi", "السامرائي": "Al-Samarrai", "التكريتي": "Al-Tikriti",
    "الطائي": "Al-Taie", "الشمري": "Al-Shammari", "المحنا": "Al-Mehanna", "الصافي": "Al-Safi",
})

LANG_NAMES = _table({
    "العربيه|عربي": "Arabic", "الانكليزيه|انكليزي|انجليزي|الانجليزيه": "English", "الكرديه|كردي": "Kurdish",
    "التركيه|تركي": "Turkish", "الفارسيه|فارسي": "Persian", "الفرنسيه|فرنسي": "French", "الالمانيه|الماني": "German",
    "الاسبانيه|اسباني": "Spanish", "الروسيه|روسي": "Russian", "الاوردو|اوردو": "Urdu", "التركمانيه|تركماني": "Turkmen",
})

NUM_WORDS = _table({
    "واحد|وحده": 1, "اثنين|ثنين|اثنان": 2, "ثلاث|ثلاثه|تلاث": 3, "اربع|اربعه": 4, "خمس|خمسه": 5, "ست|سته": 6,
    "سبع|سبعه": 7, "ثمان|ثمانيه|ثمن": 8, "تسع|تسعه": 9, "عشر|عشره": 10, "احدعش|احدى عشر": 11,
})


# ------------------------------------------------------------- transliteration
_CONS = {"ب": "b", "ت": "t", "ث": "th", "ج": "j", "ح": "h", "خ": "kh", "د": "d", "ذ": "th", "ر": "r", "ز": "z",
         "س": "s", "ش": "sh", "ص": "s", "ض": "dh", "ط": "t", "ظ": "dh", "غ": "gh", "ف": "f", "ق": "q", "ك": "k",
         "ل": "l", "م": "m", "ن": "n", "ه": "h", "گ": "g", "چ": "ch", "پ": "p", "ڤ": "v", "ء": ""}


def _translit_word(word):
    """Readable Latin spelling of one Arabic word (used only when nothing better is available)."""
    w = _DIAC.sub("", word)
    if w.startswith("ال") and len(w) > 3:
        return "Al-" + _translit_word(w[2:])
    tokens = []  # (latin, is_vowel)
    for i, ch in enumerate(w):
        prev_cons = bool(tokens) and not tokens[-1][1]
        last = i == len(w) - 1
        if ch in "اأإآى":
            tokens.append(("i" if ch == "إ" else "a", True))
        elif ch == "ة":
            tokens.append(("a", True))
        elif ch == "ه" and last and prev_cons:
            tokens.append(("a", True))
        elif ch in "وؤ":
            tokens.append(("o", True) if (prev_cons or not tokens) and i else ("w", False))
        elif ch in "يئ":
            tokens.append(("i", True) if prev_cons and (last or i) else ("y", False))
        elif ch == "ع":
            tokens.append(("A" if not tokens else ("aa" if prev_cons else "a"), True))
        elif ch in _CONS:
            tokens.append((_CONS[ch], False))
        elif ch.isascii():
            tokens.append((ch, ch.lower() in "aeiou"))
    out = ""
    for i, (latin, vowel) in enumerate(tokens):
        if out and not vowel and not tokens[i - 1][1] and latin:  # two consonants together: add a vowel
            out += "e" if i == len(tokens) - 1 else "a"
        out += latin
    out = re.sub(r"([aeiou])\1\1+", r"\1\1", out)
    return out[:1].upper() + out[1:]


def transliterate(text):
    return " ".join(_translit_word(w) if has_ar(w) else w for w in clean(text).split())


# ---------------------------------------------------------- machine translation
def mt(text):
    """Free machine translation ar->en (MyMemory). Returns English text or None."""
    text = clean(text)
    if not text:
        return ""
    with _cache_lock:
        if text in _cache:
            return _cache[text]
    chunks, cur = [], ""
    for piece in re.split(r"(?<=[.!؟?،,])\s+", text):  # MyMemory accepts up to 500 bytes per request
        if len((cur + " " + piece).encode()) > 450 and cur:
            chunks.append(cur)
            cur = piece
        else:
            cur = (cur + " " + piece).strip()
    if cur:
        chunks.append(cur)
    out = []
    for chunk in chunks:
        params = {"q": chunk, "langpair": "ar|en"}
        if os.environ.get("MYMEMORY_EMAIL", "").strip():
            params["de"] = os.environ["MYMEMORY_EMAIL"].strip()
        try:
            j = http.get(MT_URL, params=params, timeout=8).json()
            res = html.unescape((j.get("responseData") or {}).get("translatedText") or "").strip()
            if str(j.get("responseStatus")) != "200" or not res or "MYMEMORY WARNING" in res.upper() \
                    or "QUERY LENGTH LIMIT" in res.upper():
                return None
            out.append(res)
        except Exception:
            return None
    result = " ".join(out)
    if has_ar(result):  # leftovers the engine didn't translate
        result = transliterate(result)
    with _cache_lock:
        _cache[text] = result
    return result


# ------------------------------------------------------------------- per kind
def _parts(text):
    return [p.strip() for p in re.split(r"\s*[,،/\-–]\s*|\s+و\s*(?=\S)|\n", clean(text)) if p.strip()]


def _lookup(table, text):
    key = norm(text)
    if key in table:
        return table[key]
    key2 = " ".join(w[2:] if w.startswith("ال") and len(w) > 4 else w for w in key.split())
    return table.get(key2)


def _phrase(text, table=None, sentence=False):
    if not has_ar(text):
        return clean(text), "latin"
    if table is not None:
        hit = _lookup(table, text)
        if hit:
            return hit, "dict"
    res = mt(text)
    if res:
        res = res.strip().rstrip(".") if not sentence else res.strip()
        return res, "mt"
    return transliterate(text), "translit"


def name_en(text):
    """Arabic full name -> English spelling (dictionary first, never semantic translation)."""
    if not has_ar(text):
        return clean(text), "latin"
    words = norm(text).split()
    out, how, i = [], "dict", 0
    while i < len(words):
        two = " ".join(words[i:i + 2])
        if i + 1 < len(words) and two in NAMES:
            out.append(NAMES[two]); i += 2; continue
        w = words[i]
        if w in ("عبد", "ابو", "ام") and i + 1 < len(words):
            nxt = words[i + 1]
            rest = NAMES.get(nxt) or _translit_word(nxt[2:] if nxt.startswith("ال") else nxt)
            prefix = {"عبد": "Abdul", "ابو": "Abu ", "ام": "Um "}[w]
            out.append(prefix + (rest.replace("Al-", "").lower() if w == "عبد" else rest))
            how = "translit" if nxt not in NAMES else how
            i += 2; continue
        if w in NAMES:
            out.append(NAMES[w])
        elif w.startswith("ال") and w[2:] in NAMES:
            out.append("Al-" + NAMES[w[2:]])
        else:
            out.append(_translit_word(w)); how = "translit"
        i += 1
    return " ".join(out), how


def place_en(text):
    if not has_ar(text):
        return clean(text), "latin"
    whole = _lookup(PLACES, text)
    if whole:
        return whole, "dict"
    parts = _parts(text)
    if len(parts) == 1:  # "بغداد الكرخ" written without a comma
        words = parts[0].split()
        if len(words) > 1 and all(_lookup(PLACES, w) for w in words):
            parts = words
    results = [_phrase(p, PLACES) for p in parts]
    how = "dict" if all(h == "dict" for _, h in results) else ("mt" if any(h == "mt" for _, h in results) else "translit")
    return ", ".join(r for r, _ in results), how


def university_en(text):
    return _phrase(text, UNIVERSITIES)


def specialty_en(text):
    return _phrase(text, SPECIALTIES)


def title_en(text):
    return _phrase(text, TITLES)


def products_en(text):
    if not has_ar(text):
        return clean(text), "latin"
    results = [_phrase(p, PRODUCTS) for p in _parts(text)]
    how = "dict" if all(h == "dict" for _, h in results) else "mt"
    return ", ".join(r for r, _ in results), how


def _company_words(text, allow_translit):
    """'صيدلية النور' -> 'Al-Noor Pharmacy' from known words; None if a word is unknown."""
    proper, generic, title = [], [], []
    key = norm(text)
    for brand in [b for b in BRANDS if " " in b]:  # multi-word brands such as "اسيا سيل"
        if brand in key:
            proper.append(BRANDS[brand])
            key = key.replace(brand, " ")
    for w in key.split():
        if w.rstrip(".") in ("د", "دكتور", "الدكتور", "دكتوره"):
            title.append("Dr.")
        elif w in GENERIC_ORG:
            generic.append(GENERIC_ORG[w])
        elif w in BRANDS:
            proper.append(BRANDS[w])
        elif w in NAMES:
            proper.append(NAMES[w])
        elif w.startswith("ال") and w[2:] in NAMES:
            proper.append("Al-" + NAMES[w[2:]])
        elif w.startswith("لل") and "ال" + w[2:] in GENERIC_ORG:
            generic.append(GENERIC_ORG["ال" + w[2:]])
        elif allow_translit:
            proper.append(_translit_word(w))
        else:
            return None
    nouns = {"Company", "Office", "Pharmacy", "Pharmaceutical Warehouse", "Foundation", "Group", "Hospital", "Clinic",
             "Laboratory", "Center", "Complex", "Showroom", "Shop", "Scientific Office"}
    generic.sort(key=lambda g: g in nouns)  # "Al-Noor Pharmaceuticals Company", "Al-Hikma Scientific Office"
    return " ".join(title + proper + generic) or None


def company_en(text):
    if not has_ar(text):
        return clean(text), "latin"
    hit = _lookup(BRANDS, text) or _lookup(GENERIC_ORG, text) or _company_words(text, allow_translit=False)
    if hit:
        return hit, "dict"
    res = mt(text)  # good for foreign brands written in Arabic ("بيونير" -> "Pioneer")
    if res:
        return res.strip().rstrip("."), "mt"
    return _company_words(text, allow_translit=True), "translit"


def sentence_en(text):
    """A duty, course or similar sentence. Returns (english or None, how)."""
    if not has_ar(text):
        return clean(text), "latin"
    res = mt(text)
    return (res, "mt") if res else (None, "failed")


def duration_en(text):
    """'سنتين' -> '2 years', '6 اشهر' -> '6 months', '2021 - حاليا' -> '2021 - Present'."""
    raw = clean(text).translate(AR_DIGITS)
    if not has_ar(raw):
        return raw, "latin"
    t = norm(raw)
    t = re.sub(r"(لحد الان|الى الان|للان|حاليا|مستمر|لحد هسه|هسه|الان)", "Present", t)
    if re.search(r"\d{4}", t):
        return re.sub(r"\s*-\s*", " - ", re.sub(r"\s*(الى|لغايه|ل)\s*", " - ", t)).strip(), "dict"
    fixed = {"شهر": "1 month", "شهرين": "2 months", "سنه": "1 year", "سنتين": "2 years", "نص سنه": "6 months",
             "نصف سنه": "6 months", "سنه ونص": "1.5 years", "سنه ونصف": "1.5 years", "اقل من سنه": "Less than 1 year",
             "اكثر من سنه": "Over 1 year", "اسبوع": "1 week", "اسبوعين": "2 weeks"}
    if t in fixed:
        return fixed[t], "dict"
    m = re.match(r"^(\d+(?:\.\d+)?|\S+)\s*(شهر|اشهر|شهور|سنه|سنوات|سنين|اسبوع|اسابيع)", t)
    if m:
        num = m.group(1)
        num = NUM_WORDS.get(num, num) if not num.replace(".", "").isdigit() else num
        if str(num).replace(".", "").isdigit():
            unit = "month" if "شه" in m.group(2) else ("week" if "سب" in m.group(2) else "year")
            n = float(num)
            return f"{num} {unit}{'' if n == 1 else 's'}", "dict"
    return _phrase(raw)


def language_en(text):
    return _phrase(text, LANG_NAMES)


def phrase_en(text):
    """Any short phrase (a skill, a language...)."""
    return _phrase(text)
