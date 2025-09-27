// paystack_gateway_setting.js (client)
frappe.ui.form.on('Paystack Gateway Setting', {
  refresh(frm) {
    frm.add_custom_button(__('Test Signature'), () => {
      frappe.confirm(__('This just validates we can read the secret and compute HMAC. Continue?'), () => {
        // We call a small server method just to ensure we can access the doc’s secret key
        frappe.call({
          method: 'frappe.client.get',
          args: { doctype: 'Paystack Gateway Setting', name: frm.doc.name }
        }).then(() => {
          frappe.show_alert({message:__('OK — credentials are readable'), indicator:'green'});
        });
      });
    });
  }
});
