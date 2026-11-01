function rejudgeTypeOnChange(object) {
    var tests = document.getElementsByName("tests");
    for (var i = 0; i < tests.length; i++) {
        if (object.value == 'SELECTED') {
            tests[i].disabled = '';
        } else {
            tests[i].disabled = 'disabled';
        }
    }
}
